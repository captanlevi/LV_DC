#!/bin/sh
# t53 (--concurrent): run a command inside a cgroup v2 path under /sys/fs/cgroup (lvdc.slice/<tag>), as the caller.
# Makefile start_chrome's CHROME_PREFIX in a trio slot is "networkSim/in_cgroup.sh lvdc.slice/<tag>": this shell writes its own
# pid into the cgroup (root does the write: a move between cgroups needs write access at their common ancestor), then
# execs the command, so Chrome keeps exp.py's user and environment exactly as single mode launches it, and every process
# it starts (the network service included) inherits the cgroup the nft mark rule matches.
#   usage: in_cgroup.sh <cgroup path> <command> [args...]
cg="/sys/fs/cgroup/$1"
shift
[ -d "$cg" ] || { echo "in_cgroup.sh: no cgroup $cg" >&2; exit 2; }
echo $$ | sudo -n tee "$cg/cgroup.procs" > /dev/null || { echo "in_cgroup.sh: cannot join $cg" >&2; exit 3; }
exec "$@"
