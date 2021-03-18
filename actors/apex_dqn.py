import ray
from actors.replay_server import PrioritizedReplayServer
from actors.dqn_worker import DQNWorker
from actors.dqn_learner import DQNLearner
import os
import pickle
import wandb
import zmq
import pyarrow as pa
import psutil
import time
from utils.misc import get_img_from_fig
from copy import deepcopy
import torch
import numpy as np
import matplotlib.pyplot as plt


class ApeXDQN:
    """ Apex-DQN controller for learning to cut
    This class spawns remote actors (replay server, learner, multiple workers and tester) as in the standard Apex paper.
    Here goes technical description of how the distributed part works. For algorithmic details refer to ???
    Implementation principles:
    ApexDQN:
        central controller, responsible for spawning, restarting, resuming and debugging the remote actors,
        and for aggregating logs from all actors and logging them to wandb.
    PrioritizedReplayServer:
        replay server class. receives transitions from workers, sends batches to learner, receives and updates priorities.
    CutDQNLearner:
        learner class. receives batches from replay server, backprops and returns priorities.
        updates periodically all workers with model parameters.
    CutDQNWorker:
        worker and tester class. generates data for training.
        a test worker evaluates periodically the model performance.

    Except the central ApexDQN controller, all those actors runs as Ray detached actors. Each actor can be restarted
    and debugged while the others keep running. Since ray starts a separate python driver for each actor,
    an actor can be restarted with potentially updated code, while the rest of actors are still running.
    Each entity has a ray unique name, which can be used for accessing the specific actor.
    Actors communicate via zmq sockets. The learner, the replay server and the central unit binds to tcp ports,
    making those ports exclusive for the current run.
    In order to allow multiple Apex instances on a single machine, a setup routine is executed at the beginning of
    each run, binding all actors to randomly selected free tcp ports. Those ports are saved to run_dir, and can
    be reused when restarting a part of the system.
    The pid and tcp ports of the latest run are saved to run_dir, allowing tracking and cleaning zombie processes.

    The communication setup goes as follows:
    1. ApexDQN binds to apex_port, starts learner and waits for learner tcp ports.
    2. Learner in its turn connects to apex_port,
       binds to replay_server_2_learner_port and learner_2_workers_pubsub_port,
       sends its port numbers to apex_port and waits for port numbers from the replay server.
    3. Replay server connects to learner and apex ports,
       binds to learner_2_replay_server_port, workers_2_replay_server_port and replay_server_2_workers_pubsub_port,
       sends port numbers tp apex and learner ports, and starts its main loop.
    4. Workers connect to learner replay and apex port numbers and start their main loop.
    5. Learner connects to learner_2_replay_server_port, and starts its main loop.
    6. ApexDQN saves all port numbers, process pids and ray server info to run dir
       and starts its main logging loop.

    When restarting a specific actor, those ports are loaded and reused.
    When restarting the entire system, this setup routine is repeated, overriding the exisiting configuration.
    """
    def __init__(self, cfg, use_gpu=True):
        self.cfg = cfg
        self.num_workers = self.cfg["num_workers"]
        self.use_gpu = use_gpu
        self.learner_gpu = use_gpu and self.cfg.get('learner_gpu', True)
        self.worker_gpu = use_gpu and self.cfg.get('worker_gpu', False)

        # container of all ray actors
        self.actors = {f'worker_{n}': None for n in range(1, self.num_workers + 1)}
        self.actors['learner'] = None
        self.actors['replay_server'] = None
        # set run dir
        run_id = self.cfg['run_id'] if self.cfg['resume'] else wandb.util.generate_id()
        self.cfg['run_id'] = run_id
        self.cfg['run_dir'] = run_dir = os.path.join(self.cfg['rootdir'], run_id)
        if not os.path.exists(run_dir):
            os.makedirs(run_dir)
        # initialize ray server
        self.init_ray()
        # apex controller socket for receiving logs
        self.apex_socket = None
        # logging
        self.step_counter = {actor_name: -1 for actor_name in self.actors.keys() if actor_name != 'replay_server'}
        self.unfinished_steps = []
        self.logs_history = {}
        self.stats_history = {}
        self.last_logging_step = -1
        self.datasets = DQNWorker.load_data(cfg)
        self.stats_template_dict = {
            'training': {'db_auc': [], 'gap_auc': [], 'active_applied_ratio': [], 'applied_available_ratio': [], 'accuracy': [], 'f1_score': []},
            'validation': {dataset_name: {inst_idx: {} for inst_idx in range(len(dataset['instances']))} for dataset_name, dataset in self.datasets.items() if 'valid' in dataset_name}}
        self.best_performance = {dataset_name: -1 for dataset_name in self.datasets.keys() if 'valid' in dataset_name}
        self.checkpoint_filepath = os.path.join(self.cfg['run_dir'], 'apex_checkpoint.pt')
        self.print_prefix = '[Apex] '
        # reuse communication setting
        if cfg['restart']:
            assert len(self.get_ray_running_actors()) > 0, 'no running actors exist. run without --restart'
            with open(os.path.join(self.cfg['run_dir'], 'com_cfg.pkl'), 'rb') as f:
                self.cfg['com'] = pickle.load(f)
            print('loaded communication config from ', os.path.join(self.cfg['run_dir'], 'com_cfg.pkl'))

        if cfg['resume']:
            self.load_checkpoint()
        # else:
        #     com_cfg = self.find_free_ports()
        #     # pickle ports to experiment dir
        #     with open(os.path.join(self.cfg['run_dir'], 'com_cfg.pkl'), 'wb') as f:
        #         pickle.dump(com_cfg, f)
        #     print('saved ports to ', os.path.join(self.cfg['run_dir'], 'com_cfg.pkl'))
        # self.cfg['com'] = com_cfg

    def init_ray(self):
        if self.cfg['restart']:
            # load ray server address from run_dir
            with open(os.path.join(self.cfg['run_dir'], 'ray_info.pkl'), 'rb') as f:
                ray_info = pickle.load(f)
            # connect to the existing ray server
            ray_info = ray.init(ignore_reinit_error=True, address=ray_info['redis_address'])

        else:
            if self.cfg['resume']:
                # check first that there are no processes running with the current run_id
                running_processes = self.get_actors_running_process()
                if any(running_processes.values()):
                    self.print('running processes found for this run_id:')
                    for actor_name, process in running_processes.items():
                        if process is not None:
                            print(f'{actor_name}: pid {process.pid}')
                    self.print('run again with --restart --force-restart to kill the existing processes')
                    exit(0)

            # create a new ray server.
            ray_info = ray.init()  # todo - do we need ignore_reinit_error=True to launch several ray servers concurrently?
            # save ray info for reconnecting
            with open(os.path.join(self.cfg['run_dir'], 'ray_info.pkl'), 'wb') as f:
                pickle.dump(ray_info, f)

        self.cfg['ray_info'] = ray_info
        time.sleep(self.cfg.get('ray_init_sleep', 0))

    def find_free_ports(self):
        """ finds free ports for all actors and returns a dictionary of all ports """
        ports = {}
        # replay server
        context = zmq.Context()
        learner_2_replay_server_socket = context.socket(zmq.PULL)
        workers_2_replay_server_socket = context.socket(zmq.PULL)
        data_request_pub_socket = context.socket(zmq.PUB)
        replay_server_2_learner_socket = context.socket(zmq.PULL)
        params_pub_socket = context.socket(zmq.PUB)
        ports["learner_2_replay_server_port"] = learner_2_replay_server_socket.bind_to_random_port('tcp://127.0.0.1', min_port=self.cfg['min_port'], max_port=self.cfg['min_port'] + self.cfg['port_range'])
        ports["workers_2_replay_server_port"] = workers_2_replay_server_socket.bind_to_random_port('tcp://127.0.0.1', min_port=self.cfg['min_port'], max_port=self.cfg['min_port'] + self.cfg['port_range'])
        ports["replay_server_2_workers_pubsub_port"] = data_request_pub_socket.bind_to_random_port('tcp://127.0.0.1', min_port=self.cfg['min_port'], max_port=self.cfg['min_port'] + self.cfg['port_range'])
        ports["replay_server_2_learner_port"] = replay_server_2_learner_socket.bind_to_random_port('tcp://127.0.0.1', min_port=self.cfg['min_port'], max_port=self.cfg['min_port'] + self.cfg['port_range'])
        ports["learner_2_workers_pubsub_port"] = params_pub_socket.bind_to_random_port('tcp://127.0.0.1', min_port=self.cfg['min_port'], max_port=self.cfg['min_port'] + self.cfg['port_range'])
        learner_2_replay_server_socket.close()
        workers_2_replay_server_socket.close()
        data_request_pub_socket.close()
        replay_server_2_learner_socket.close()
        params_pub_socket.close()

        return ports

    def setup(self):
        """
        Instantiate all components as Ray detached Actors.
        Detached actors have global unique names, they run independently of the current python driver.
        Detached actors can be killed and restarted, with potentially updated code.
        Use case: when debugging/upgrading the learner/tester code, while the replay server keeps running.
        For reference see: https://docs.ray.io/en/master/advanced.html#dynamic-remote-parameters
        the "Detached Actors" section.
        In the setup process actors incrementally bind to random free ports,
        to allow multiple instances running on the same node.
        """
        assert 'com' not in self.cfg.keys()
        # open main logger socket for receiving logs from all actors
        context = zmq.Context()
        self.apex_socket = context.socket(zmq.PULL)
        apex_port = self.apex_socket.bind_to_random_port('tcp://127.0.0.1', min_port=10000, max_port=60000)
        self.cfg['com'] = {'apex_port': apex_port}
        self.print(f"binding to {apex_port} for receiving logs")

        # spawn learner
        self.print('spawning learner process')
        ray_learner = ray.remote(num_gpus=int(self.learner_gpu))(DQNLearner)  # , num_cpus=2
        # instantiate learner and run its io process in a background thread
        self.actors['learner'] = ray_learner.options(name='learner').remote(hparams=self.cfg, use_gpu=self.learner_gpu, run_io=True, run_setup=True)
        # wait for learner's com config
        learner_msg = self.apex_socket.recv()
        topic, body = pa.deserialize(learner_msg)
        assert topic == 'learner_com_cfg'
        for k, v in body:
            self.cfg['com'][k] = v

        # spawn replay server
        self.print('spawning replay server process')
        ray_replay_server = ray.remote(PrioritizedReplayServer)
        self.actors['replay_server'] = ray_replay_server.options(name='replay_server').remote(config=self.cfg, run_setup=True)
        # todo go to replay_server, connect to apex port. bind to others, send com config, and start run
        # wait for replay_server's com config
        replay_server_msg = self.apex_socket.recv()
        topic, body = pa.deserialize(replay_server_msg)
        assert topic == 'replay_server_com_cfg'
        for k, v in body:
            self.cfg['com'][k] = v

        # spawn workers and tester
        self.print('spawning workers and tester processes')
        ray_worker = ray.remote(num_gpus=int(self.worker_gpu), num_cpus=1)(DQNWorker)
        for n in range(1, self.num_workers + 1):
            self.actors[f'worker_{n}'] = ray_worker.options(name=f'worker_{n}').remote(n, hparams=self.cfg, use_gpu=self.worker_gpu)

        # pickle com config to experiment dir
        with open(os.path.join(self.cfg['run_dir'], 'com_cfg.pkl'), 'wb') as f:
            pickle.dump(self.cfg['com'], f)
        self.print(f'saving communication config to {os.path.join(self.cfg["run_dir"], "com_cfg.pkl")}')

        # initialize wandb logger
        # todo wandb
        self.print('initializing wandb')
        wandb_config = self.cfg.copy()
        wandb_config.pop('datasets')
        wandb_config.pop('com')
        wandb_config.pop('ray_info')
        if self.cfg['wandb_offline']:
            os.environ['WANDB_API_KEY'] = 'd1e669477d060991ed92264313cade12a7995b3d'
            os.environ['WANDB_MODE'] = 'dryrun'
        wandb.init(resume='allow',
                   id=self.cfg['run_id'],
                   project=self.cfg['project'],
                   config=wandb_config,
                   tags=self.cfg['tags'])

        # save pid to run_dir
        pid = os.getpid()
        pid_file = os.path.join(self.cfg["run_dir"], 'apex_pid.txt')
        self.print(f'[Apex] saving pid {pid} to {pid_file}')
        with open(pid_file, 'w') as f:
            f.writelines(str(pid) + '\n')
        self.print('setup finished')

    def train(self):
        print("[Apex] running logger loop")
        # ready_ids, remaining_ids = ray.wait([actor.run.remote() for actor in self.actors.values()])
        for actor in self.actors.values():
            actor.run.remote()
        self.wandb_loop()

    def wandb_loop(self):
        # todo refactor - receive eval results from all workers, organize and log to wandb
        while True:
            # receive message
            packet = self.apex_socket.recv()
            topic, sender, global_step, body = pa.deserialize(packet)
            assert topic == 'log'
            assert self.step_counter[sender] < global_step
            # increment sender step counter
            self.step_counter[sender] = global_step

            # check if packet is outdated
            if global_step <= self.last_logging_step:
                self.print(
                    f'Outdated packet from {sender} discarded (last logging step = {self.last_logging_step}, packet step = {global_step})')
                continue

            # put things into dictionaries
            # create entry:
            if global_step not in self.stats_history.keys():
                self.stats_history[global_step] = deepcopy(self.stats_template_dict)

            log_dict = {}
            if sender == 'learner':
                stats, model_params = body
                for k, v in stats:
                    log_dict[k] = v
                # store params for checkpointing best models
                # for param, new_param in zip(self.stats_history[global_step]['policy_net'].parameters(), model_params):
                #     new_param = torch.FloatTensor(new_param)
                #     param.data.copy_(new_param)
                self.stats_history[global_step]['params'] = model_params

            else:
                assert 'worker' in sender
                training_stats, validation_stats = body
                # training_stats = {k: v for k, v in training_stats}
                # todo concat training stats to existing step
                    # if type(v) == tuple and v[0] == 'fig':
                    #     log_dict[k] = wandb.Image(v[1], caption=k)
                    # else:
                    #     log_dict[k] = v

                # update stats
                for k, v in training_stats:
                    self.stats_history[global_step]['training'][k] += v
                for k_v_list in validation_stats:
                    stats_dict = {k: v for k, v in k_v_list}
                self.stats_history[global_step]['validation'][stats_dict['dataset_name']][stats_dict['inst_idx']][stats_dict['scip_seed']] = stats_dict


            # update logs
            if len(log_dict) > 0:
                if global_step in self.logs_history.keys():
                    self.logs_history[global_step].update(log_dict)
                else:
                    self.logs_history[global_step] = log_dict

            # push to pending logs
            if not self.unfinished_steps or global_step > self.unfinished_steps[-1]:
                self.unfinished_steps.append(global_step)

            # if all actors finished a certain step, log this step to wandb.
            # wait for late actors up to 10 steps. after that, late packets will be discarded.
            if len(self.unfinished_steps) > 50:  # todo return to 1000
                self.print('some actor is dead. restart to continue logging.')
                print(self.step_counter)
            while self.unfinished_steps and (all([self.unfinished_steps[0] <= cnt for cnt in self.step_counter.values()]) or len(self.unfinished_steps) > 50):
                step = self.unfinished_steps.pop(0)
                # todo - finish step, create figures, average stats and return log_dict
                log_dict = self.finish_step(step)
                wandb.log(log_dict, step=step)
                self.last_logging_step = step
                self.save_checkpoint()

    def finish_step(self, step):
        print_msg = f'Step: {step}'
        log_dict = self.logs_history.pop(step)
        stats = self.stats_history.pop(step)
        # average training stats and add to log dict
        for k, values in stats['training'].items():
            if len(values) > 0:
                log_dict[f'training/{k}'] = np.mean(values)

        # process validation results
        for dataset_name, dataset_stats in stats['validation'].items():
            dataset = self.datasets[dataset_name]
            # todo:
            #  init figures
            #  compute average values and add to log_dict
            #  plot gap/db vs lp iterations with baselines
            #  plot similarity to scip bars
            #  compute average improvement of db/gap AUC (with/out early stops)
            #  track best models, checkpoint policy net and save figures
            #  print log line
            #  return log_dict

            all_values = {
                'db_auc': [],
                'db_auc_improvement': [],
                'gap_auc': [],
                'gap_auc_improvement': [],
                'active_applied_ratio': [],
                'applied_available_ratio': [],
                'accuracy': [],
                'f1_score': [],
            }
            db_auc_without_early_stops = []
            gap_auc_without_early_stops = []
            for inst_stats in dataset_stats.values():
                for seed_stats in inst_stats.values():
                    # add stats to average values
                    for k, v_list in all_values.items():
                        v_list.append(seed_stats[k])
                    if seed_stats['terminal_state'] != 'NODE_LIMIT':
                        db_auc_without_early_stops.append(seed_stats['db_auc'])
                        gap_auc_without_early_stops.append(seed_stats['gap_auc'])

            # if there are validation results, then compute averages and plot curves
            if all([len(v) > 0 for v in all_values.values()]):
                # compute averages
                avg_values = {k: np.mean(v) for k, v in all_values.items()}
                avg_values['db_auc_without_early_stops'] = np.mean(db_auc_without_early_stops)
                avg_values['gap_auc_without_early_stops'] = np.mean(gap_auc_without_early_stops)

                # add plots
                col_labels = [f'Seed={seed}' for seed in dataset['scip_seed']]
                row_labels = [f'inst {inst_idx}' for inst_idx in range(dataset['num_instances'])]
                figures = init_figures(nrows=dataset['num_instances'], ncols=len(dataset['scip_seed']), row_labels=row_labels, col_labels=col_labels)
                for (inst_idx, inst_stats), (G, baseline) in zip(dataset_stats.items(), dataset['instances']):
                    for seed_idx, (scip_seed, seed_stats) in enumerate(inst_stats.items()):
                        add_subplot(figures, inst_idx, seed_idx, seed_stats, baseline, scip_seed, dataset, avg_values)
                finish_figures(figures)

                # update log_dict
                log_dict.update({f'{dataset_name}/{k}': v for k, v in avg_values.items()})
                log_dict.update({f'{dataset_name}/{figname}': wandb.Image(get_img_from_fig(figures[figname]['fig'], dpi=300), caption=figname) for figname in figures['fignames']})
                print_msg += '\t| {}: {}_imp={}'.format(dataset_name, self.cfg["dqn_objective"], avg_values[self.cfg["dqn_objective"]+"_improvement"])

            # if all validation results are ready, then
            # save model and figures if its performance is the best till now
            if all([len(v) == 30 for v in all_values.values()]):
                cur_perf = avg_values[self.cfg['dqn_objective']]
                if cur_perf > self.best_performance[dataset_name]:
                    self.best_performance[dataset_name] = cur_perf
                    for figname in figures['fignames']:
                        figures[figname]['fig'].savefig(os.path.join(self.cfg['run_dir'], f'best_{self.dataset_name}_{figname}.png'))
                    with open(os.path.join(self.cfg['run_dir'], f'best_{self.dataset_name}_params.pkl'), 'wb') as f:
                        pickle.dump(stats['params'], f)
        self.print(print_msg)
        return log_dict

    def save_checkpoint(self):
        torch.save({
            'step_counter': self.step_counter,
            'last_logging_step': self.last_logging_step,
            'best_performance': self.best_performance,
        }, self.checkpoint_filepath)

    def load_checkpoint(self):
        try:
            checkpoint = torch.load(self.checkpoint_filepath)
            self.step_counter = checkpoint['step_counter']
            self.last_logging_step = checkpoint['last_logging_step']
            self.best_performance = checkpoint['best_performance']
            self.print('loaded checkpoint from ', self.checkpoint_filepath)
        except:
            self.print('did not find checkpoint file. starting from scratch')

    def get_actors_running_process(self, actors=None):
        actors = actors if actors is not None else list(self.actors.keys()) + ['apex']
        running_actors = {}
        for actor_name in actors:
            # read existing pid from run dir
            with open(os.path.join(self.cfg['run_dir'], actor_name + '_pid.txt'), 'r') as f:
                pid = int(f.readline().strip())
            try:
                os.kill(pid, 0)
            except OSError:
                print(f'{actor_name} process does not exist')
                running_actors[actor_name] = None
            else:
                print(f'found {actor_name} process (pid {pid})')
                running_actors[actor_name] = psutil.Process(pid)
        return running_actors

    def get_ray_running_actors(self, actors=None):
        actors = actors if actors is not None else list(self.actors.keys())
        running_actors = {}
        for actor_name in actors:
            if actor_name == 'apex':
                continue
            try:
                actor = ray.get_actor(actor_name)
                running_actors[actor_name] = actor
            except ValueError as e:
                # if actor_name doesn't exist, ray will raise a ValueError exception saying this
                print(e)
                running_actors[actor_name] = None
        return running_actors

    def restart(self, actors=[], force_restart=False):
        """ restart actors as remote entities """
        actors = list(self.actors.keys()) + ['apex'] if len(actors) == 0 else actors
        # move 'apex' to the end of list
        if 'apex' in actors:
            actors.append(actors.pop(actors.index('apex')))

        ray_running_actors = self.get_ray_running_actors(actors)
        actor_processes = self.get_actors_running_process(actors)

        ray_worker = ray.remote(num_gpus=int(self.worker_gpu), num_cpus=1)(DQNWorker)
        ray_learner = ray.remote(num_gpus=int(self.learner_gpu))(DQNLearner)  # , num_cpus=2
        ray_replay_server = ray.remote(PrioritizedReplayServer)
        handles = []
        # restart all actors
        for actor_name, actor_process in actor_processes.items():
            # actor_process = running_actors[actor_name]
            if actor_process is not None or ray_running_actors.get(actor_name, None) is not None:
                if force_restart:
                    print(f'killing {actor_name} process (pid {actor_process.pid if actor_process is not None else "?"})')
                    if actor_name == 'apex':
                        actor_process.kill()
                    else:
                        ray.kill(ray_running_actors[actor_name])
                else:
                    print(f'{actor_name} is running (pid {actor_process.pid if actor_process is not None else ray_running_actors[actor_name]}). '
                          f'use --force-restart for killing running actors and restarting new ones.')
                    continue

            print(f'restarting {actor_name}...')
            if actor_name == 'learner':
                learner = ray_learner.options(name='learner').remote(hparams=self.cfg, use_gpu=self.learner_gpu, run_io=True)
                handles.append(learner.run.remote())
            elif actor_name == 'replay_server':
                replay_server = ray_replay_server.options(name='replay_server').remote(config=self.cfg)
                handles.append(replay_server.run.remote())
            elif 'worker' in actor_name:
                prefix, worker_id = actor_name.split('_')
                worker_id = int(worker_id)
                assert prefix == 'worker' and worker_id in range(1, self.num_workers + 1)
                worker = ray_worker.options(name=actor_name).remote(worker_id, hparams=self.cfg, use_gpu=self.worker_gpu)
                handles.append(worker.run.remote())
            elif actor_name == 'apex':
                # bind to apex_port
                context = zmq.Context()
                self.apex_socket = context.socket(zmq.PULL)
                self.apex_socket.bind(f'tcp://127.0.0.1:{self.cfg["com"]["apex_port"]}')
                # initialize wandb logger
                self.print('initializing wandb')
                wandb_config = self.cfg.copy()
                wandb_config.pop('datasets')
                wandb_config.pop('com')
                wandb_config.pop('ray_info')
                wandb.init(resume='allow',  # hparams['resume'],
                           id=self.cfg['run_id'],
                           project=self.cfg['project'],
                           config=wandb_config)

                # save pid to run_dir
                pid = os.getpid()
                pid_file = os.path.join(self.cfg["run_dir"], 'apex_pid.txt')
                self.print('saving pid {pid} to {pid_file}')
                with open(pid_file, 'w') as f:
                    f.writelines(str(pid) + '\n')
                self.print('starting wandb loop')
                self.wandb_loop()

        # # todo - skip this when debugging an actor
        # if len(handles) > 0:
        #     ready_ids, remaining_ids = ray.wait(handles)
        #     # todo - find a good way to block the main program here, so ray will continue tracking all actors, restart etc.
        #     ray.get(ready_ids + remaining_ids, timeout=self.cfg.get('time_limit', 3600 * 48))
        print('finished')

    def run_debug(self, actor_name):
        # spawn all the other actors as usual
        rest_of_actors = [name for name in self.actors.keys() if name != actor_name]
        self.restart(actors=rest_of_actors)

        # debug actor locally
        # kill the existing one if any
        try:
            actor = ray.get_actor(actor_name)
            # if actor exists, kill it
            print(f'killing the existing {actor_name}...')
            ray.kill(actor)

        except ValueError as e:
            # if actor_name doesn't exist, ray will raise a ValueError exception saying this
            print(e)

        print(f'instantiating {actor_name} locally for debug...')
        if actor_name == 'learner':
            learner = DQNLearner(hparams=self.cfg, use_gpu=self.learner_gpu, gpu_id=self.cfg.get('gpu_id', None), run_io=True)
            learner.run()
        elif actor_name == 'replay_server':
            replay_server = PrioritizedReplayServer(config=self.cfg)
            replay_server.run()
        else:
            prefix, worker_id = actor_name.split('_')
            worker_id = int(worker_id)
            assert prefix == 'worker' and worker_id in range(1, self.num_workers + 1)
            worker = DQNWorker(worker_id, hparams=self.cfg, use_gpu=self.worker_gpu, gpu_id=self.cfg.get('gpu_id', None))
            worker.run()

    def run(self):
        self.setup()
        self.train()

    def print(self, expr):
        print(self.print_prefix, expr)


def init_figures(nrows=10, ncols=3, col_labels=['seed_i']*3, row_labels=['graph_i']*10):
    figures = {}
    fignames = ['Dual_Bound_vs_LP_Iterations', 'Gap_vs_LP_Iterations', 'Similarity_to_SCIP']
    for figname in fignames:
        fig, axes = plt.subplots(nrows, ncols, sharex=True, sharey=True, squeeze=False)
        fig.set_size_inches(w=8, h=10)
        fig.set_tight_layout(True)
        figures[figname] = {'fig': fig, 'axes': axes}
    figures['nrows'] = nrows
    figures['ncols'] = ncols
    figures['col_labels'] = col_labels
    figures['row_labels'] = row_labels
    figures['fignames'] = fignames
    return figures


def add_subplot(figures, row, col, dqn_stats, baseline, scip_seed, dataset, avg_values=None):
    """
    plot the last episode curves to subplot in position (row, col)
    plot dqn agent dualbound/gap curves together with the baseline curves.
    should be called after each validation/test episode with row=graph_idx, col=seed_idx
    """
    dataset_stats = dataset['stats']
    lp_iterations_limit = dataset['lp_iterations_limit']
    # dqn_stats = self.episode_stats
    bsl_0 = baseline['rootonly_stats'][scip_seed]
    bsl_1 = baseline['10_random'][scip_seed]
    bsl_2 = baseline['10_most_violated'][scip_seed]

    # set labels for the last subplot
    if avg_values is not None:
        db_labels = ['DQN {:.4f}({:.4f})'.format(avg_values['db_auc'], avg_values['db_auc_without_early_stops']),
                     'SCIP {:.4f}'.format(dataset_stats['rootonly_stats']['db_auc_avg']),
                     '10 RANDOM {:.4f}'.format(dataset_stats['10_random']['db_auc_avg']),
                     '10 MOST VIOLATED {:.4f}'.format(dataset_stats['10_most_violated']['db_auc_avg']),
                     'OPTIMAL'
                     ]
        gap_labels = ['DQN {:.4f}({:.4f})'.format(avg_values['gap_auc'], avg_values['gap_auc_without_early_stops']),
                      'SCIP {:.4f}'.format(dataset_stats['rootonly_stats']['gap_auc_avg']),
                      '10 RANDOM {:.4f}'.format(dataset_stats['10_random']['gap_auc_avg']),
                      '10 MOST VIOLATED {:.4f}'.format(dataset_stats['10_most_violated']['gap_auc_avg']),
                      'OPTIMAL'
                      ]
    else:
        db_labels = [None] * 5
        gap_labels = [None] * 5

    for db_label, gap_label, color, lpiter, db, gap in zip(db_labels, gap_labels,
                                                           ['b', 'g', 'y', 'c', 'k'],
                                                           [dqn_stats['lp_iterations'], bsl_0['lp_iterations'], bsl_1['lp_iterations'], bsl_2['lp_iterations'], [0, lp_iterations_limit]],
                                                           [dqn_stats['dualbound'], bsl_0['dualbound'], bsl_1['dualbound'], bsl_2['dualbound'], [baseline['optimal_value']]*2],
                                                           [dqn_stats['gap'], bsl_0['gap'], bsl_1['gap'], bsl_2['gap'], [0, 0]]
                                                           ):
        if lpiter[-1] < lp_iterations_limit:
            # extend curve to the limit
            lpiter = lpiter + [lp_iterations_limit]
            db = db + db[-1:]
            gap = gap + gap[-1:]
        assert lpiter[-1] == lp_iterations_limit
        # plot dual bound and gap, marking early stops with red borders
        ax = figures['Dual_Bound_vs_LP_Iterations']['axes'][row, col]
        ax.plot(lpiter, db, color, label=db_label)
        if dqn_stats['terminal_state'] == 'NODE_LIMIT':
            for spine in ax.spines.values():
                spine.set_edgecolor('red')
        ax = figures['Gap_vs_LP_Iterations']['axes'][row, col]
        ax.plot(lpiter, gap, color, label=gap_label)
        if dqn_stats['terminal_state'] == 'NODE_LIMIT':
            for spine in ax.spines.values():
                spine.set_edgecolor('red')

    # plot similarity to scip bars
    total_ncuts = dqn_stats['true_pos'] + dqn_stats['true_neg'] + dqn_stats['false_pos'] + dqn_stats['false_neg']
    rects = []
    ax = figures['Similarity_to_SCIP']['axes'][row, col]
    rects += ax.bar(-0.3, dqn_stats['true_pos'] / total_ncuts, width=0.2, label='true pos')
    rects += ax.bar(-0.1, dqn_stats['true_neg'] / total_ncuts, width=0.2, label='true neg')
    rects += ax.bar(+0.1, dqn_stats['false_pos'] / total_ncuts, width=0.2, label='false pos')
    rects += ax.bar(+0.3, dqn_stats['false_neg'] / total_ncuts, width=0.2, label='false neg')

    """Attach a text label above each bar in *rects*, displaying its height."""
    for rect in rects:
        height = rect.get_height()
        ax.annotate('{:.2f}'.format(height),
                    xy=(rect.get_x() + rect.get_width() / 2, height),
                    xytext=(0, 3),  # 3 points vertical offset
                    textcoords="offset points",
                    ha='center', va='bottom')
    ax.set_xticks([], [])  # disable x ticks


def finish_figures(figures):
    nrows, ncols = figures['nrows'], figures['ncols']
    for figname in figures['fignames']:
        # add col labels at the first row only
        for col in range(ncols):
            ax = figures[figname]['axes'][0, col]
            ax.set_title(figures['col_labels'][col])
        # add row labels at the first col only
        for row in range(nrows):
            ax = figures[figname]['axes'][row, 0]
            ax.set_ylabel(figures['row_labels'][row])
        # add legend to the bottom-left subplot only
        ax = figures[figname]['axes'][-1, -1]
        ax.legend(loc='upper center', bbox_to_anchor=(0.5, -0.5), fancybox=True, shadow=True, ncol=1, borderaxespad=0.)
