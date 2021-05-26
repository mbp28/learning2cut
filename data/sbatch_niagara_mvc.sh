#!/bin/bash
#SBATCH --time=12:00:00
#SBATCH --account=def-alodi
#SBATCH --output=gen-trainset-%j.out
#SBATCH --job-name=generate_dataset-%j
#SBATCH --mem=0
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=40

# load modules and activate virtualenv
module load NiaEnv/2018a\n
module load python\n
source $HOME/server_bashrc\n
source $HOME/venv/bin/activate\n

# generate dataset
srun python generate_data.py --experiment_configfile ../experiments/cut_selection_dqn/configs/exp5.yaml --data_configfile mvc_data_config.yaml --datadir $SCRATCH/learning2cut/data --mp ray --nworkers 79



