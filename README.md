# Learning to cut with SCIP
This repository contains reinforcement learning environments
for playing with cut selection in SCIP, 
as well as code for extensive preliminary experiments with our baselines. 
The project is described in detail in my Master's thesis on 
Combinatorial Optimization with Graph Reinforcement Learning.  

You will find here:
- RL Environments for 
  - Adapting SCIP cut selection parameters to individual ILP instances.
  - Adapting SCIP cut selection parameters to every LP round. 
  - Direct cut selection, i.e explicitly choosing the cuts which will b applied to the LP in each LP round.
- Distributed Apex-DQN framework for training with debug capability of remote actors.  
- For Compute Canada users:  
  - Scripts and guidelines how to run multiple whole node experiments exploiting the massive compute power provided by the various clusters.
  - Complementary experiments showing the room for improvement on MAXCUT and MVC.
  

The project requires a modified version of `scipoptsuite-6.0.2` which
will be publicly available soon.  
We are working on integrating this work into Ecole and providing a fancy
gym-like clean environment, 
but however, if you are interested in direct control of every line in SCIP, 
this project will provide you with good starting point and many workarounds.

This implementation is based on `scipoptsuite-6.0.2` and `PySCIPOpt`,

# Acknowledgements
This work was done during my Master's studies at the Technion
under the supervision of Prof. Shie Mannor and Prof. Tamir Hazan.
The project was developed together with @gasse and @akazachk from the Polytechnique university in Montreal.
I thank @dchetelat for his help in the initial steps of the project, 
and in particular for improving the mathematical formulation of the MAXCUT problem.
I also thank @cyoon1729 for providing me with initial Ape-X DQN implementation.

## Installation  
0. Clone this repo, create a `virtualenv` and install requirements:  
> git clone https://github.com/avrech/learning2cut.git  
> virtualenv --python=python3 venv  
> source venv/bin/activate
> pip install -r learning2cut/requirements.txt  

1. Append `export SCIPOPTDIR=/home/my-scip` to your `~/.bashrc`.  
2. Clone and install my `scipoptsuite-6.0.2` version (includes some extra features needed for the RL environment):  
> git clone https://github.com/avrech/scipoptsuite-6.0.2-avrech.git  
> cd scipoptsuite-6.0.2-avrech  
> cmake -Bbuild -H. -DCMAKE_BUILD_TYPE=Debug -DCMAKE_INSTALL_PREFIX=$SCIPOPTDIR  
> cmake --build build  
> cd build  
> make install  

3. Install my branch on PySCIPOpt  
> git clone https://github.com/ds4dm/PySCIPOpt.git  
> cd PySCIPOpt  
> git checkout ml-cutting-planes  
> pip install --debug_option='--debug' .  

4. Follow the instructions [here](https://pytorch-geometric.readthedocs.io/en/latest/notes/installation.html) to install `torch_geometric`.  

5. Install the rest of requirements  
> cd learning2cut  
> pip install -r requirements.txt  

6. Sign up to [Weights & Biases](https://www.wandb.com/), and follow the [instructions](https://docs.wandb.com/quickstart) to connect your device to your `wandb` account. 

## Running on Compute Canada
### Graham
* `pyarrow` cannot be installed directly, and must be loaded using `module load arrow`.  
* `torch_geometric` is compiled for specific `torch` and `cuda` versions. For available `torch` versions contact CC support. 
* The following setup was tested successfully on Graham:
> $ module load StdEnv/2018.3 gcc/7.3.0 python/3.7.4 arrow  
$ virtualenv env && source env/bin/activate  
(env) ~ $ pip install torch==1.4.0 torch_geometric torchvision torch-scatter torch-sparse torch-cluster torch-spline-conv -U --force  
(env) ~ $ python -c "import pyarrow; torch_geometric; import torch_cluster; import torch_cluster.graclus_cpu"  
(env) ~ $  
### Niagara
* The virtualenv worked with `module load NiaEnv/2018a` inside the sbatch scripts.   


## Reproducing Datasets  
Inside `learning2cut/data` run:  
> python generate_data.py --experiment_configfile ../experiments/cut_selection_dqn/configs/exp5.yaml --data_configfile <mvc/maxcut>_data_config.yaml --datadir <DATADIR> --mp ray --nworkers <NWORKERS> --quiet

Or on `Niagara` (recommended):  

> sbatch sbatch_niagara_maxcut.sh  
> sbatch sbatch_niagara_mvc.sh  

`generate_data.py` does the following:  
- Randomizes `barabasi-albert` and `Erdos-Reyni` graphs  for MAXCUT and MVC respectively.  
- For each graph, solves MAXCUT/MVC with B&C with time limit of 1 hour.  
- Saves stats of the solving process for three baselines `default`, `15_random` and `15_most_violated`.  


## Experiment 1 - Room for Improvement
This experiment requires massive computation power. 
The code was built to run on Niagara.  
### Import data
Inside `learning2cut/experiments/room4improvement` run:  
> python run_experiment.py  --datadir $SCRATCH/learning2cut/data --rootdir $SCRATCH/room4improvement 

The script will save the first instance of each validation set generated by `data/generate_data.py` 
into a `data.pkl` for the whole experiment.    

### Compute `scip_tuned` baseline
On Niagara, inside `learning2cut/experiments/room4improvement` run:   
> python run_scip_tuned.py --rootdir $SCRATCH/room4improvement --nnodes 20 --ncpus_per_node 80  

Jobs for finding `scip_tuned` policy will be submitted. After all jobs have finished, run the same command line again to finalize stuff. 
In a case something went wrong in the first run, the script should be invoked again until it finishes the work.      

### Compute `scip_adaptive` baseline
On Niagara, inside `learning2cut/experiments/room4improvement` run:  
> python run_scip_adaptive.py --rootdir $SCRATCH/room4improvement --nnodes 20 --ncpus_per_node 80  

Running this command line `2K` times will generate adaptive policy for `K` lp rounds. 

### Compute `scip_tuned_avg` baseline 
On Niagara, inside `learning2cut/experiments/room4improvement` run:    
> python run_scip_tuned_avg.py --rootdir $SCRATCH/room4improvement --datadir $SCRATCH/learning2cut/data --nnodes 20 --ncpus_per_node 60  

After all jobs finish run again to finalize stuff. 

### Evaluate all baselines ###
To compare all baselines in terms of solving time, 
run again `run_experiment` pointing to the rootdir where
`scip_tuned`, `scip_tuned_avg` and `scip_adaptive` results are stored. 
The script will test all baselines on the local machine one by one
without multiprocessing. 
Results will be saved to a csv and png files. 

## Experiment 2 - Learning SCIP Separating Parameters
In this experiment we investigate two RL formulations for tuning SCIP, 
combinatorial contextual multi-armed bandit (CCMAB) and MDP.  
Actually, since we do not really have standard bandits setting, i.e
partial returns for each arm and a joint reward function of those returns,
we call it CCMAB but in fact optimize 1-step MDPs.
To run these two experiments on compute canada, enter
`experiments/scip_tuning_dqn`, and take a look in `sbatch_scip_tuning.py`. 
This script allows submitting multiple whole node jobs to Graham or Niagara. 
Inside this script edit the parameters grid search space you want to test. 
This can be useful for both basic hparam tuning, 
and for running multiple training jobs with different seeds for robustness 
evaluation of the RL algorithm.   
The following command line will submit multiple jobs, saving their output to 
a path depending on the cluster you use. 
Niagara example:  
> python sbatch_scip_tuning.py --cluster niagara --tag reported_runs --hours 12

Synchronize the `wandb` run dirs to wandb cloud once the jobs finished:
> python ../sync_wandb_runs.py --dir $SCRATCH/learning2cut/scip_tuning/results/reported_runs/outfiles/

Select `run_id`s to test and evaluate them on the test sets by:  
> python sbatch_scip_tuning.py --cluster niagara --hours 3 --test --run_ids 130m0n5o  3n3uxetn baseline --tag reported_runs --num_test_nodes 10 && python sbatch_scip_tuning.py --cluster niagara --hours 3 --test --run_ids 130m0n5o  3n3uxetn baseline --tag reported_runs --num_test_nodes 10 --test_args use_cycles=False,set_aggressive_separation=False

This will execute extensive tests on tons of cpus, saving their results in `test` directories inside each `run_dir`
Run again after all jobs completed:
> python sbatch_scip_tuning.py --cluster niagara --hours 3 --test --run_ids 130m0n5o  3n3uxetn baseline --tag reported_runs --num_test_nodes 10 && python sbatch_scip_tuning.py --cluster niagara --hours 3 --test --run_ids 130m0n5o  3n3uxetn baseline --tag reported_runs --num_test_nodes 10 --test_args use_cycles=False,set_aggressive_separation=False

and analyize the results with:

> python --savedir $SCRATCH/learning2cut/scip_tuning/results/reported_runs --mdp_run_id 3n3uxetn --ccmab_run_id 130m0n5o
> python --savedir $SCRATCH/learning2cut/scip_tuning/results/reported_runs --mdp_run_id 3n3uxetn --ccmab_run_id 130m0n5o --test_args use_cycles=False,set_aggressive_separation=False

This will summarize root only episode results and full branch and cut results, and write everything to `.csv` files.

## Running Experiments
### Single run 
There are two run files, `run_single_thread_dqn.py` for single thread training, and `run_apex_dqn.py` for distributed training.
The distributed version is useful also for debugging and development, as each actor can run independently of the others. 
`run_apex_dqn.py` allows debugging and updating the code of a specific actor while the entire system keep running. 
Run
> python run_apex_dqn.py --rootdir /path/to/save/results --configfile /path/to/config/file --use-gpu  

Example config files can be found at `learning2cut/experiments/dqn/configs`. Those files conveniently pack parameters for training. 
All parameters are controlled also from command line, where the command line args override the config file setting. 
Each run is assigned a random 8-characters `run_id` which can be used for resuming and for viewing results on `wandb` dashboard. 

### Resuming
For resuming a run, add `--resume --run_id <run_id>` to the command line arguments. 

### Restarting Actors
Actors can be restarted (for updating code) without restarting the entire system. Useful cases:
* updating the tester code with additional tests/logs without shutting down the replay server.  
* fixing bugs and restarting an actor after crashing.  
Restart options are:
* Restarting the entire system: add `--restart` to the resuming command line. This will restart all crashed actors. 
* Restarting specific actors: add `--restart --restart-actors <list of actors>`. The list of actors can include any combination of `apex`, `replay_server`, `learner`, `tester` and `worker_<worker_id>` (`worker_id` running from 1 to `num_workers`).   
* Forcing restart when the target actors are still running: add `--force-restart` to the arguments above.  
Example:

### Debugging Remote Actors
In order to debug a remote actors, run:
> python run_apex_dqn.py --resume --run_id <run_id> --restart [--restart-actors <list of actors>] --debug-actor <actor_name>  

This will restart the debugged actor main loop in the debugger, so one can step into the actor code, while the rest of remote actors keep running.  


## Experiments
### Cycles Variability
Inside `learning2cut/experiments/dqn` run:  
> python cycles_variability.py --logdir results/cycles_variability [--simple_cycle_only --chordless_only --enable_chordality_check] --record_cycles  

`cycles_variability.py` will solve each graph in `validset_20_30` and `validset_50_60` 10 times with seeds ranging from 0 to 9. In each separation round it will save the cycles generated along with other related stats.  
The script will pickle a dictionary of the following structure:  
```
{dataset: [{seed: stats for seed in range(10)} for graph in dataset] for dataset in [`validset_20_30`, `validset_50_60`]}  
```  
The `recorded_cycles` are stored in `stats` alongside the `dualbound`, `lp_iterations` etc. A cycle is stored as a dictionary with items:
- `edges`: a list of the edges in cycle  
- `F`: a list of odd number of cut edges  
- `C_minus_F`: a list of the rest of the edges  
- `is_simple`: True if the cycle is simple cycle else False  
- `is_chordless`: True if the cycle has no chords else False  
- `applied`: True if the cycle was selected to the LP else False  

### Experiment 1
Inside `learning2cut/experiments/dqn` run:  
> python run_apex_dqn.py --rootdir results/exp1 --configfile configs/exp1-overfitVal25-demoLossOnly-fixedTrainingScipSeed.yaml --use-gpu  

### Experiment 2
Inside `learning2cut/experiments/dqn` run:  
> python run_apex_dqn.py --use-gpu --rootdir results/exp2 --configfile configs/exp2-overfitVal25-demoLossOnly.yaml




|Done |Exp | Train Set | Behaviour | Loss | SCIP Seed  | Goal | Results |
|---|:---:|:---:|:---:|:---:|:---:|:---|:---:|
| &#9745; |1 | Fixed graph| Demo | Demo | Fixed | Perfect overfitting | [here](https://app.wandb.ai/avrech/learning2cut/runs/2v0lez39)|  
| &#9745; |2 | Fixed graph| Demo | Demo | Random | Generalization across seeds | [here](https://app.wandb.ai/avrech/learning2cut/runs/3i8f068p)|  
| &#9745; |3 | Random | Demo | Demo | Random | Generalization across graphs | [here](https://app.wandb.ai/avrech/learning2cut/runs/dyvqmmp9)|  
| &#9744; |4 | Random | Demo | Demo+DQN | Random | See convergence to "interesting" policy | [here](https://app.wandb.ai/avrech/learning2cut/runs/1jmcareo)|
| &#9744; |5 | Random | Demo+DQN| Demo+DQN | Random | Improving over SCIP | [here](https://wandb.ai/avrech/learning2cut/runs/1jmcareo?workspace=user-avrech)|

