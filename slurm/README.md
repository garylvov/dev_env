For students who work with me at Brown, remember that:

# With Great Power Comes Great Responsibility.

Please do not hog cluster resources. 
Only use cluster resources for academic research.
Please do not use more than 8 GPUs at one time. 

Always check current cluster utilization, with ``bash check_util.bash`` and ``bash rank_users.bash``

Only then, run ``bash allocate.bash``.

If I see you consistently using many GPUs, I will definitely ask why ;) 

For people looking at this external to Brown, I hard-coded the partition and some of the GPU tags, but I think this could work for you too with a few small modifications.
