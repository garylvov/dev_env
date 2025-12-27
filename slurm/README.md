
## Note for External People

For people looking at this external to Brown, I hard-coded the partition and some of the GPU tags, but I think this could work for you too with a few small modifications.

## Note for students who work with me at Brown

**With Great Power Comes Great Responsibility.**

Please do not hog cluster resources. 
Only use cluster resources for academic research.
Please do not use more than 8 GPUs at one time. 
If I see you consistently using many GPUs, I will definitely ask why ;) 

Always check current cluster utilization, with ``bash check_util.bash`` and ``bash rank_users.bash``

Only then, run ``bash allocate.bash``.

If the node isn't immediately allocated, you can check it's status in a new window with the following.

```
squeue -u $USER
```

Also, when on node, I often increase the process limit with the following.

```
ulimit -u 8192
```

For using more than one window, with the same compute node, is as easy as follows.

```
squeue -u $USER # read the compute node name
ssh <COMPUTE_NODE_NAME> # e.g: ssh gpu2503
```
