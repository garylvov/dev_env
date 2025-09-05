# DEVeloper ENVironment (Ubuntu) 
This is a collection of personal tweaks/rituals that make Ubuntu feel just right for me like how a ballerina customizes their pointe shoes to feel just right for them, as well as some other useful resources I keep going back to.

It would be cool if this was an Ansible playbook, but I haven't made it there yet. 
This also has some handy commands that I always look up and then forget.

# GPU Drivers with Docker Passthrough - Make ML Go Brrr

[The definitive NVIDIA Ubuntu Driver / CUDA Install Guide](https://github.com/garylvov/dev_env/tree/main/setup_scripts/nvidia)

# Hermetic and Reproducible Python Package Template

[Hermetic and Reproducible Python Package Template](https://github.com/garylvov/pixidock_template) authored by yours truly.


## The Classics
```
sudo apt-get install terminator # Make sure to turn on infinite scrollback: right click->preferences->profiles->scrolling
sudo apt-get install htop
sudo apt-get install nvtop
sudo apt-get install btop
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

## Keybindings 

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

## Local LLM 

Running Ollama: 
```
docker rm ollama 2>/dev/null || true && docker run -d --gpus=all -v ollama:/root/.ollama -p 11434:11434 --name ollama ollama/ollama
docker exec -it ollama bash
# Some favorites of mine:
ollama run deepseek-r1:70b
ollama run qwen:110b
ollama run qwen2.5-coder:32b

# Example of increasing context size:
echo \
"FROM deepseek-r1:70b
PARAMETER num_ctx 60000
PARAMETER num_predict 30000" > Modelfile
ollama create deepseek-r1-60k-context-and-30k:70b -f Modelfile
ollama run deepseek-r1-60k-context-and-30k:70b --verbose

# Afterwards, make sure to clean up with CTRL + C, CTRL + D, and then docker stop ollama
```

## Browser Stuff
[Vimium](https://vimium.github.io/)

## Space Preservation Part I 

Check disk usage with the following.
```
sudo apt-get install baobab
sudo baobab
```

If your ```overlay2``` folder gets huge, see the following.

## When Docker Gets Greedy (Space Preservation Part II)

Get rid of old docker images with the following.

```
docker system df
# Clear builder cache with the following.
docker builder prune -a -f
```


## Kubernetes 
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

## Tmux

[Yank](https://github.com/tmux-plugins/tmux-yank)

## Fan stuff for PCs

[Cooler Control](https://github.com/codifryed/coolercontrol/tree/main)

## Github

[Adding a new SSH agent](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/generating-a-new-ssh-key-and-adding-it-to-the-ssh-agent)

## Notes
[Obsidian](https://obsidian.md/)

[Obsidian Git Plugin](https://publish.obsidian.md/git-doc/Start+here)

[Obsidian Ubuntu 24.04 Permissions](https://askubuntu.com/questions/1512287/obsidian-appimage-the-suid-sandbox-helper-binary-was-found-but-is-not-configu)

Run Obsidian with the ``--disable-gpu`` flag to [prevent any glitches with Wayland](https://www.reddit.com/r/hyprland/comments/1aphbfq/comment/krv1np6/?utm_source=share&utm_medium=web3x&utm_name=web3xcss&utm_term=1&utm_content=share_button)!

## Citations

[Zotero](https://www.zotero.org/download/)

[Extra Zotero Install Help](https://www.zotero.org/support/installation)


## Python Extract Method

Some VSCode Python extensions conflict with each other. I forgot how to resolve this. I think you may want just the Microsoft Python extension and as few others as possible. I also really like the rainbow indent extension it's great!
