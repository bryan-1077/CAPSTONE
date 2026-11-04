#!/bin/bash
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=6
#SBATCH --time=00:30:00
#SBATCH --partition=adademic
#SBATCH --qos=olympus-academic
#SBATCH --job-name=lec-matching-retry
#SBATCH --output=/mnt/nfs-scratch/ECEN_403-404/ddr4_capstone/CAPSTONE/shared/lec_matching_20261103_01/job.log
source ~/.bashrc
export PATH="/mnt/nfs-scratch/ECEN_403-404/ddr4_capstone/CAPSTONE/tools/oss-cad-suite/bin:$PATH"
python3 -u /mnt/nfs-scratch/ECEN_403-404/ddr4_capstone/CAPSTONE/shared/lec_matching_20261103_01/runner.py --run
