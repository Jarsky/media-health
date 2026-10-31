# appended to /etc/bash.bashrc: completion, prompt and welcome for interactive shells
. /etc/media-health-completion.bash
PS1='\[\e[1;38;2;255;43;214m\]media-health\[\e[0m\]:\[\e[38;2;34;211;238m\]\w\[\e[0m\]\$ '
[ -z "$MH_MOTD_SHOWN" ] && export MH_MOTD_SHOWN=1 && media-health --motd
