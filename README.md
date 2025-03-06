# DEVeloper ENVironment (Ubuntu) 
This is a collection of personal tweaks/rituals that make Ubuntu feel just right for me like how a ballerina customizes their pointe shoes to feel just right for them.
It would be cool if this was an Ansible playbook, but I haven't made it there yet. 
This also has some handy commands that I always look up and then forget.

## GPU Drivers with Docker Passthrough - Make ML Go Brrr
[The definitive NVIDIA Ubuntu Driver / CUDA Install Guide](https://github.com/garylvov/dev_env/tree/main/setup_scripts/nvidia)

## The Classics
```
sudo apt-get install terminator # Make sure to turn on infinite scrollback: right click->preferences->profiles->scrolling
sudo apt-get install htop
sudo apt-get install nvtop
```

### Code Editors

[Cursor Stuff](https://gist.github.com/evgenyneu/5c5c37ca68886bf1bea38026f60603b6)
```
sudo snap install code --classic
sudo apt-get install vim
```

## Gnome Extensions 
Caffeine and Tactile are must have extensions IMO
```
sudo apt-get install gnome-shell-extension-manager
```

## Keybindings Stuff

I've used Keyd in the past but tbh I didn't love it and am considering switching to Hawck. I think binding the capslock key to something that isn't useless is great.

[Hawck](https://github.com/snyball/hawck)

[Keyd](https://github.com/rvaiya/keyd)

Keyd conf file:

```
[ids]
*
[main]
capslock = layer(capslock)
[capslock]
e = oneshot(control)
q = oneshot(alt)
o = oneshot(shift)
p = oneshot(tab)
g = b
h = left
j = down
k = up
l = right
u = S-'
i = S-5
```

## Remoting in
[NoMachine](https://www.nomachine.com/)
```
sudo apt-get install openssh-client
sudo apt-get install openssh-server
```

# Browser Stuff
[Vimium](https://vimium.github.io/)

## Kubernetes Stuff 
[Kubectl install](https://kubernetes.io/docs/tasks/tools/install-kubectl-linux/)

[K9s](https://github.com/derailed/k9s):
```
wget https://github.com/derailed/k9s/releases/download/v0.32.7/k9s_linux_amd64.deb && apt install ./k9s_linux_amd64.deb && rm k9s_linux_amd64.deb
```

Handy commands that I always forget:
```
kubectl config get-contexts # Get all contexts
kubectl get namespaces # Get all namespaces
kubectl config use-context <CONTEXT> # Set Contexts
kubectl config set-context --current --namespace=<NAMESPACE> # Set the namespace for the current context
```
