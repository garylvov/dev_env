srun --partition=3090-gcondo \
     --gres=gpu:rtx3090:8 \
     --cpus-per-task=8 \
     --mem=64G \
     --time=08:00:00 \
     --nodes=1 \
     --pty bash

