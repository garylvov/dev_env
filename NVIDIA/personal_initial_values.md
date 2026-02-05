# Here are the default values for power, clock, and memory freq for my GPUs, just for personal ref

# I power limit, clock limit, and mem limit, with persistence mode, so just for ref
```
GPU 01:00.0 → Power: 480W (max 480W) | Core: up to 2115 MHz | Memory: up to 10501 MHz
GPU 21:00.0 → Power: 420W (max 450W) | Core: up to 2130 MHz | Memory: up to 9751 MHz
GPU C1:00.0 → Power: 420W (max 450W) | Core: up to 2130 MHz | Memory: up to 9751 MHz
GPU E1:00.0 → Power: 450W (max 450W) | Core: up to 2115 MHz | Memory: up to 10501 MHz
```

Full output:

```
garylvov@minerva:~$ nvidia-smi -q -d POWER

==============NVSMI LOG==============

Timestamp                                 : Wed Feb  4 22:19:26 2026
Driver Version                            : 580.65.06
CUDA Version                              : 13.0

Attached GPUs                             : 4
GPU 00000000:01:00.0
    GPU Power Readings
        Average Power Draw                : 13.44 W
        Instantaneous Power Draw          : 13.37 W
        Current Power Limit               : 480.00 W
        Requested Power Limit             : 480.00 W
        Default Power Limit               : 480.00 W
        Min Power Limit                   : 100.00 W
        Max Power Limit                   : 480.00 W
    Power Samples
        Duration                          : 117.96 sec
        Number of Samples                 : 119
        Max                               : 14.21 W
        Min                               : 13.22 W
        Avg                               : 13.57 W
    GPU Memory Power Readings 
        Average Power Draw                : N/A
        Instantaneous Power Draw          : N/A
    Module Power Readings
        Average Power Draw                : N/A
        Instantaneous Power Draw          : N/A
        Current Power Limit               : N/A
        Requested Power Limit             : N/A
        Default Power Limit               : N/A
        Min Power Limit                   : N/A
        Max Power Limit                   : N/A

GPU 00000000:21:00.0
    GPU Power Readings
        Average Power Draw                : 12.98 W
        Instantaneous Power Draw          : 13.05 W
        Current Power Limit               : 420.00 W
        Requested Power Limit             : 420.00 W
        Default Power Limit               : 420.00 W
        Min Power Limit                   : 100.00 W
        Max Power Limit                   : 450.00 W
    Power Samples
        Duration                          : 117.97 sec
        Number of Samples                 : 119
        Max                               : 13.61 W
        Min                               : 12.40 W
        Avg                               : 13.06 W
    GPU Memory Power Readings 
        Average Power Draw                : N/A
        Instantaneous Power Draw          : N/A
    Module Power Readings
        Average Power Draw                : N/A
        Instantaneous Power Draw          : N/A
        Current Power Limit               : N/A
        Requested Power Limit             : N/A
        Default Power Limit               : N/A
        Min Power Limit                   : N/A
        Max Power Limit                   : N/A

GPU 00000000:C1:00.0
    GPU Power Readings
        Average Power Draw                : 9.52 W
        Instantaneous Power Draw          : 9.57 W
        Current Power Limit               : 420.00 W
        Requested Power Limit             : 420.00 W
        Default Power Limit               : 420.00 W
        Min Power Limit                   : 100.00 W
        Max Power Limit                   : 450.00 W
    Power Samples
        Duration                          : 117.96 sec
        Number of Samples                 : 119
        Max                               : 10.41 W
        Min                               : 9.01 W
        Avg                               : 9.62 W
    GPU Memory Power Readings 
        Average Power Draw                : N/A
        Instantaneous Power Draw          : N/A
    Module Power Readings
        Average Power Draw                : N/A
        Instantaneous Power Draw          : N/A
        Current Power Limit               : N/A
        Requested Power Limit             : N/A
        Default Power Limit               : N/A
        Min Power Limit                   : N/A
        Max Power Limit                   : N/A

GPU 00000000:E1:00.0
    GPU Power Readings
        Average Power Draw                : 31.91 W
        Instantaneous Power Draw          : 34.27 W
        Current Power Limit               : 450.00 W
        Requested Power Limit             : 450.00 W
        Default Power Limit               : 450.00 W
        Min Power Limit                   : 100.00 W
        Max Power Limit                   : 450.00 W
    Power Samples
        Duration                          : 3.96 sec
        Number of Samples                 : 119
        Max                               : 41.37 W
        Min                               : 29.18 W
        Avg                               : 32.18 W
    GPU Memory Power Readings 
        Average Power Draw                : N/A
        Instantaneous Power Draw          : N/A
    Module Power Readings
        Average Power Draw                : N/A
        Instantaneous Power Draw          : N/A
        Current Power Limit               : N/A
        Requested Power Limit             : N/A
        Default Power Limit               : N/A
        Min Power Limit                   : N/A
        Max Power Limit                   : N/A

garylvov@minerva:~$ nvidia-smi -q -d CLOCK

==============NVSMI LOG==============

Timestamp                                 : Wed Feb  4 22:19:38 2026
Driver Version                            : 580.65.06
CUDA Version                              : 13.0

Attached GPUs                             : 4
GPU 00000000:01:00.0
    Clocks
        Graphics                          : 210 MHz
        SM                                : 210 MHz
        Memory                            : 405 MHz
        Video                             : 555 MHz
    Applications Clocks
        Graphics                          : N/A
        Memory                            : N/A
    Default Applications Clocks
        Graphics                          : N/A
        Memory                            : N/A
    Deferred Clocks
        Memory                            : N/A
    Max Clocks
        Graphics                          : 2115 MHz
        SM                                : 2115 MHz
        Memory                            : 10501 MHz
        Video                             : 1965 MHz
    Max Customer Boost Clocks
        Graphics                          : N/A
    SM Clock Samples
        Duration                          : Not Found
        Number of Samples                 : Not Found
        Max                               : Not Found
        Min                               : Not Found
        Avg                               : Not Found
    Memory Clock Samples
        Duration                          : Not Found
        Number of Samples                 : Not Found
        Max                               : Not Found
        Min                               : Not Found
        Avg                               : Not Found
    Clock Policy
        Auto Boost                        : N/A
        Auto Boost Default                : N/A

GPU 00000000:21:00.0
    Clocks
        Graphics                          : 210 MHz
        SM                                : 210 MHz
        Memory                            : 405 MHz
        Video                             : 555 MHz
    Applications Clocks
        Graphics                          : N/A
        Memory                            : N/A
    Default Applications Clocks
        Graphics                          : N/A
        Memory                            : N/A
    Deferred Clocks
        Memory                            : N/A
    Max Clocks
        Graphics                          : 2130 MHz
        SM                                : 2130 MHz
        Memory                            : 9751 MHz
        Video                             : 1965 MHz
    Max Customer Boost Clocks
        Graphics                          : N/A
    SM Clock Samples
        Duration                          : Not Found
        Number of Samples                 : Not Found
        Max                               : Not Found
        Min                               : Not Found
        Avg                               : Not Found
    Memory Clock Samples
        Duration                          : Not Found
        Number of Samples                 : Not Found
        Max                               : Not Found
        Min                               : Not Found
        Avg                               : Not Found
    Clock Policy
        Auto Boost                        : N/A
        Auto Boost Default                : N/A

GPU 00000000:C1:00.0
    Clocks
        Graphics                          : 210 MHz
        SM                                : 210 MHz
        Memory                            : 405 MHz
        Video                             : 555 MHz
    Applications Clocks
        Graphics                          : N/A
        Memory                            : N/A
    Default Applications Clocks
        Graphics                          : N/A
        Memory                            : N/A
    Deferred Clocks
        Memory                            : N/A
    Max Clocks
        Graphics                          : 2130 MHz
        SM                                : 2130 MHz
        Memory                            : 9751 MHz
        Video                             : 1965 MHz
    Max Customer Boost Clocks
        Graphics                          : N/A
    SM Clock Samples
        Duration                          : Not Found
        Number of Samples                 : Not Found
        Max                               : Not Found
        Min                               : Not Found
        Avg                               : Not Found
    Memory Clock Samples
        Duration                          : Not Found
        Number of Samples                 : Not Found
        Max                               : Not Found
        Min                               : Not Found
        Avg                               : Not Found
    Clock Policy
        Auto Boost                        : N/A
        Auto Boost Default                : N/A

GPU 00000000:E1:00.0
    Clocks
        Graphics                          : 210 MHz
        SM                                : 210 MHz
        Memory                            : 405 MHz
        Video                             : 555 MHz
    Applications Clocks
        Graphics                          : N/A
        Memory                            : N/A
    Default Applications Clocks
        Graphics                          : N/A
        Memory                            : N/A
    Deferred Clocks
        Memory                            : N/A
    Max Clocks
        Graphics                          : 2115 MHz
        SM                                : 2115 MHz
        Memory                            : 10501 MHz
        Video                             : 1965 MHz
    Max Customer Boost Clocks
        Graphics                          : N/A
    SM Clock Samples
        Duration                          : Not Found
        Number of Samples                 : Not Found
        Max                               : Not Found
        Min                               : Not Found
        Avg                               : Not Found
    Memory Clock Samples
        Duration                          : Not Found
        Number of Samples                 : Not Found
        Max                               : Not Found
        Min                               : Not Found
        Avg                               : Not Found
    Clock Policy
        Auto Boost                        : N/A
        Auto Boost Default                : N/A


```


Then the cult classic

```
garylvov@minerva:~$ nvidia-smi
Wed Feb  4 22:24:03 2026       
+-----------------------------------------------------------------------------------------+
| NVIDIA-SMI 580.65.06              Driver Version: 580.65.06      CUDA Version: 13.0     |
+-----------------------------------------+------------------------+----------------------+
| GPU  Name                 Persistence-M | Bus-Id          Disp.A | Volatile Uncorr. ECC |
| Fan  Temp   Perf          Pwr:Usage/Cap |           Memory-Usage | GPU-Util  Compute M. |
|                                         |                        |               MIG M. |
|=========================================+========================+======================|
|   0  NVIDIA GeForce RTX 3090 Ti     On  |   00000000:01:00.0 Off |                  Off |
|  0%   34C    P8             13W /  480W |      18MiB /  24564MiB |      0%      Default |
|                                         |                        |                  N/A |
+-----------------------------------------+------------------------+----------------------+
|   1  NVIDIA GeForce RTX 3090        On  |   00000000:21:00.0 Off |                  N/A |
|  0%   25C    P8             13W /  420W |      18MiB /  24576MiB |      0%      Default |
|                                         |                        |                  N/A |
+-----------------------------------------+------------------------+----------------------+
|   2  NVIDIA GeForce RTX 3090        On  |   00000000:C1:00.0 Off |                  N/A |
|  0%   26C    P8              9W /  420W |      18MiB /  24576MiB |      0%      Default |
|                                         |                        |                  N/A |
+-----------------------------------------+------------------------+----------------------+
|   3  NVIDIA GeForce RTX 3090 Ti     On  |   00000000:E1:00.0  On |                  Off |
|  0%   39C    P8             30W /  450W |     792MiB /  24564MiB |      0%      Default |
|                                         |                        |                  N/A |
+-----------------------------------------+------------------------+----------------------+

+-----------------------------------------------------------------------------------------+
| Processes:                                                                              |
|  GPU   GI   CI              PID   Type   Process name                        GPU Memory |
|        ID   ID                                                               Usage      |
|=========================================================================================|
|    0   N/A  N/A            4995      G   /usr/bin/gnome-shell                      4MiB |
|    1   N/A  N/A            4995      G   /usr/bin/gnome-shell                      4MiB |
|    2   N/A  N/A            4995      G   /usr/bin/gnome-shell                      4MiB |
|    3   N/A  N/A            4995      G   /usr/bin/gnome-shell                    467MiB |
|    3   N/A  N/A            5955      G   /usr/bin/Xwayland                        10MiB |
|    3   N/A  N/A            6777      G   ...ersion=20260204-010045.692000        106MiB |
|    3   N/A  N/A           18034      G   gnome-text-editor                        94MiB |
+-----------------------------------------------------------------------------------------+
```
