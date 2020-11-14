import json
import os
import pathlib
import sys
import threading
import traceback
from datetime import datetime
from signal import signal, SIGCHLD, SIGHUP, SIGINT, SIGTERM, setitimer, SIGALRM, ITIMER_REAL

import click
import sh
# noinspection PyUnresolvedReferences
from sh import podman

SERVICES_BASE_PATH = "/docker/services/"

sdnotify = sh.Command("systemd-notify")


class PodKeeper:
    def __init__(self, network, identifier):
        self.podnet_args = ("--network", network) if network else ()
        identifier_path = pathlib.PurePath(identifier)
        if len(identifier_path.parts) != 1:
            raise ValueError(f"identifier has too many parts: {identifier_path}")
        self.podhome = pathlib.Path(SERVICES_BASE_PATH) / identifier_path
        if not self.podhome.exists():
            raise NotADirectoryError(f"pod home does not exist: {self.podhome}")
        self.podname = f"{identifier}_pod"
        self.podyaml = f"pod-{identifier}.yaml"
        podyaml_complete = (self.podhome / self.podyaml)
        if not podyaml_complete.exists():
            raise FileNotFoundError(f"pod definition does not exist: {podyaml_complete}")
        self.stopping = threading.Event()
        self.reloading = threading.Event()
        self.checking = threading.Event()
        self.waiter = threading.Event()

    def destroy(self, signum, stackframe):
        print("Destroy signal", signum, file=sys.stderr, flush=True)
        self.stopping.set()
        self.waiter.set()

    def reload(self, signum, stackframe):
        print("Reload signal", signum, file=sys.stderr, flush=True)
        self.reloading.set()
        self.waiter.set()

    def check(self, signum, stackframe):
        print("Check signal", signum, file=sys.stderr, flush=True)
        self.checking.set()
        self.waiter.set()

    def run(self):
        os.chdir(self.podhome)
        last_check = datetime.utcnow()
        print(f"Starting pod {self.podname} at {last_check}", file=sys.stderr, flush=True)
        podman.play.kube(self.podyaml, *self.podnet_args)
        try:
            if 'NOTIFY_SOCKET' in os.environ:
                sdnotify("--ready")

            while not self.stopping.is_set():
                self.waiter.wait()
                self.waiter.clear()
                if self.checking.is_set():
                    self.checking.clear()
                    new_timestamp = datetime.utcnow()
                    pod_description = json.loads(podman.pod.inspect(self.podname))
                    for container in pod_description["Containers"]:
                        if container["State"] != "running":
                            print(f"Container {container['Name']} exited", file=sys.stderr, flush=True)
                            logs = podman.logs('--since', last_check.isoformat(), container['Name'])
                            print(f"Log since last check:\n{logs}", file=sys.stderr, flush=True)
                            self.stopping.set()
                    last_check = new_timestamp

                if self.reloading.is_set():
                    self.reloading.clear()
                    print("Reloading pod", self.podname, file=sys.stderr, flush=True)
                    try:
                        podman.pod.kill("--signal", "HUP", self.podname)
                    except sh.ErrorReturnCode:
                        print("Error reloading pod", file=sys.stderr, flush=True)
                        traceback.print_exc()

        finally:
            self.stop_sequence()

    def stop_sequence(self):
        print("Stopping pod", self.podname, file=sys.stderr, flush=True)
        try:
            podman.pod.stop("-t", "19", self.podname)
            successful_stopped = True
        except sh.ErrorReturnCode:
            print(f"First stop of {self.podname} was not successful!", file=sys.stderr, flush=True)
            successful_stopped = False
        try:
            podman.pod.stop("-t", "5", self.podname)
        except sh.ErrorReturnCode:
            if not successful_stopped:
                print(f"Second stop of {self.podname} was not successful!", file=sys.stderr, flush=True)
        try:
            podman.pod.rm(self.podname)
        except sh.ErrorReturnCode:
            print(f"Removal of {self.podname} was not successful!", file=sys.stderr, flush=True)


@click.command()
@click.option("--network", default="brodge", help="Network for the created pod")
@click.argument("identifier")
def main(network, identifier):
    keeper = PodKeeper(network, identifier)

    signal(SIGINT, keeper.destroy)
    signal(SIGTERM, keeper.destroy)
    signal(SIGHUP, keeper.reload)
    signal(SIGCHLD, keeper.check)
    signal(SIGALRM, keeper.check)
    setitimer(ITIMER_REAL, 4.0, 10.0)

    keeper.run()


if __name__ == '__main__':
    main()
