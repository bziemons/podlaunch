import json
import os
import pathlib
import sys
import threading
import traceback
from datetime import datetime
from signal import signal, SIGHUP, SIGINT, SIGTERM, setitimer, SIGALRM, ITIMER_REAL

import click
import sh
# noinspection PyUnresolvedReferences
from sh import podman

SERVICES_BASE_PATH = "/docker/services/"

sdnotify = sh.Command("systemd-notify")


class PodKeeper:
    def __init__(self, network, stop_previous, identifier):
        self.podnet_args = ("--network", network) if network else ()
        self.stop_previous = stop_previous
        identifier_path = pathlib.PurePath(identifier)
        if len(identifier_path.parts) != 1:
            raise ValueError(f"identifier has path parts: {identifier_path}")
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
        self.last_check = datetime.utcnow()

    def destroy(self, signum, stackframe):
        print("Destroy signal", signum, file=sys.stderr, flush=True)
        self.stopping.set()
        self.waiter.set()

    def reload(self, signum, stackframe):
        print("Reload signal", signum, file=sys.stderr, flush=True)
        self.reloading.set()
        self.waiter.set()

    def check(self, signum, stackframe):
        self.checking.set()
        self.waiter.set()

    def run(self):
        os.chdir(self.podhome)
        if self.stop_previous and podman.pod.exists(self.podname).exit_code == 0:
            print(f"Stopping pod {self.podname}", file=sys.stderr, flush=True)
            podman.pod.stop(self.podname)

        print(f"Starting pod {self.podname} at {self.last_check}", file=sys.stderr, flush=True)
        podman.play.kube(self.podyaml, *self.podnet_args)
        try:
            if 'NOTIFY_SOCKET' in os.environ:
                sdnotify("--ready", f"--pid={os.getpid()}", "--status=Monitoring pod...")

            while not self.stopping.is_set():
                self.waiter.wait()
                self.waiter.clear()

                if self.checking.is_set():
                    self.checking.clear()
                    self.check_pod()

                if self.reloading.is_set():
                    self.reloading.clear()
                    self.reload_pod()

            if 'NOTIFY_SOCKET' in os.environ:
                sdnotify("--status=Stopping pod")
        finally:
            self.stop_pod()

    def reload_pod(self):
        print("Reloading pod", self.podname, file=sys.stderr, flush=True)
        try:
            podman.pod.kill("--signal", "HUP", self.podname)
        except sh.ErrorReturnCode:
            print("Error reloading pod", file=sys.stderr, flush=True)
            traceback.print_exc()

    def check_pod(self):
        new_timestamp = datetime.utcnow()
        inspect_command = podman.pod.inspect(self.podname)
        pod_description = json.loads(inspect_command.stdout)
        for container in pod_description["Containers"]:
            if container["State"] != "running":
                print(f"Container {container['Name']} exited", file=sys.stderr, flush=True)
                logs = podman.logs('--since', self.last_check.isoformat(), container['Name'])
                print(f"Log since last check:\n{logs}", file=sys.stderr, flush=True)
                self.stopping.set()
        self.last_check = new_timestamp

    def stop_pod(self):
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
@click.option("--stop-previous", default=True, help="Stop previously running pod with the same name")
@click.argument("identifier")
def main(network, stop_previous, identifier):
    keeper = PodKeeper(network, stop_previous, identifier)

    signal(SIGINT, keeper.destroy)
    signal(SIGTERM, keeper.destroy)
    signal(SIGHUP, keeper.reload)
    signal(SIGALRM, keeper.check)
    setitimer(ITIMER_REAL, 4.0, 10.0)

    keeper.run()


if __name__ == '__main__':
    main()
