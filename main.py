import json
import logging
import os
import pathlib
import sys
import threading
import traceback
from datetime import datetime, timedelta
from queue import SimpleQueue
from signal import signal, SIGHUP, SIGINT, SIGTERM, setitimer, SIGALRM, ITIMER_REAL, SIGUSR1, SIGUSR2, strsignal

import click
import sh
from sh import podman

SERVICES_BASE_PATH = "/docker/services/"

# noinspection PyCallingNonCallable
shlog = sh(_out=sys.stdout, _err=sys.stderr)
sdnotify = sh.Command("systemd-notify")


class PodKeeper:
    def __init__(self, network, log_driver, log_level, replace, remove, identifier):
        self.podnet_args = ()
        self.podnet_args += ("--network", network) if network else ()
        self.podnet_args += ("--log-driver", log_driver) if log_driver else ()
        self.podnet_args += ("--log-level", log_level) if log_level else ()
        self.replace = replace
        self.remove = remove
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
        self.passing_signal = threading.Event()
        self.pass_signal_nums = SimpleQueue()

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

    def passthrough(self, signum, stackframe):
        self.pass_signal_nums.put(item=signum, block=True, timeout=3)
        self.passing_signal.set()
        self.waiter.set()

    def run(self):
        os.chdir(self.podhome)
        if self.replace and podman.pod.exists(self.podname, _ok_code=[0, 1]).exit_code == 0:
            print(f"Replacing existing pod {self.podname}", file=sys.stderr, flush=True)
            shlog.podman.pod.stop(self.podname)
            shlog.podman.pod.rm("-f", self.podname)

        print(f"Starting pod {self.podname} at {self.last_check}", file=sys.stderr, flush=True)
        shlog.podman.play.kube(self.podyaml, *self.podnet_args)
        try:
            shlogger = logging.getLogger("sh.command")
            oldlevel = shlogger.level
            shlogger.setLevel(logging.ERROR)

            if 'NOTIFY_SOCKET' in os.environ:
                sdnotify("--ready", f"--pid={os.getpid()}", "--status=Monitoring pod...")

            while not self.stopping.is_set():
                self.waiter.wait()
                self.waiter.clear()

                if self.passing_signal.is_set():
                    self.passing_signal.clear()
                    while not self.pass_signal_nums.empty():
                        signum = self.pass_signal_nums.get(block=True, timeout=2)
                        self.signal_pod(signum)

                if self.checking.is_set():
                    self.checking.clear()
                    self.check_pod()

                if self.reloading.is_set():
                    self.reloading.clear()
                    self.signal_pod(SIGHUP)

            if 'NOTIFY_SOCKET' in os.environ:
                sdnotify("--status=Stopping pod")

            logging.getLogger("sh.command").setLevel(oldlevel)
        finally:
            self.stop_pod()

    def signal_pod(self, signum):
        print(f"Sending signal '{strsignal(signum)}' to pod {self.podname}", file=sys.stderr, flush=True)
        try:
            shlog.podman.pod.kill("--signal", str(signum), self.podname)
        except sh.ErrorReturnCode:
            print("Error signaling pod", file=sys.stderr, flush=True)
            traceback.print_exc()

    def check_pod(self):
        new_timestamp = datetime.utcnow()
        inspect_command = podman.pod.inspect(self.podname)
        pod_description = json.loads(inspect_command.stdout)
        for container in pod_description["Containers"]:
            if container["State"] != "running":
                print(f"Container {container['Name']} exited", file=sys.stderr, flush=True)
                logs_since = self.last_check - timedelta(seconds=10)
                print(f"Log since last check (-10s):\n", file=sys.stderr, flush=True)
                shlog.podman.logs('--since', logs_since.isoformat(), container['Name'], _out=sys.stderr)
                self.stopping.set()
        self.last_check = new_timestamp

    def stop_pod(self):
        print("Stopping pod", self.podname, file=sys.stderr, flush=True)
        try:
            shlog.podman.pod.stop("-t", "19", self.podname)
            successful_stopped = True
        except sh.ErrorReturnCode:
            print(f"First stop of {self.podname} was not successful!", file=sys.stderr, flush=True)
            successful_stopped = False
        try:
            shlog.podman.pod.stop("-t", "5", self.podname)
        except sh.ErrorReturnCode:
            if not successful_stopped:
                print(f"Second stop of {self.podname} was not successful!", file=sys.stderr, flush=True)

        if self.remove:
            try:
                shlog.podman.pod.rm(self.podname)
            except sh.ErrorReturnCode:
                print(f"Removal of {self.podname} was not successful!", file=sys.stderr, flush=True)


@click.command()
@click.option("--network", default="brodge", help="Network for the created pod")
@click.option("--log-driver", default="journald", help="Logging driver for the created pod")
@click.option("--log-level", default="", help="Controls log-level on podman call")
@click.option("--replace/--no-replace", default=True, help="Controls replacement of previously running pod with the "
                                                           "same name")
@click.option("--remove/--keep", default=True, help="Controls removal of pod after stopping")
@click.argument("identifier")
def main(network, log_driver, log_level, replace, remove, identifier):
    logging.basicConfig(level=logging.INFO)

    keeper = PodKeeper(
        network=network,
        log_driver=log_driver,
        log_level=log_level,
        replace=replace,
        remove=remove,
        identifier=identifier
    )

    signal(SIGINT, keeper.destroy)
    signal(SIGTERM, keeper.destroy)
    signal(SIGHUP, keeper.reload)
    signal(SIGALRM, keeper.check)
    signal(SIGUSR1, keeper.passthrough)
    signal(SIGUSR2, keeper.passthrough)
    setitimer(ITIMER_REAL, 4.0, 10.0)

    keeper.run()


if __name__ == '__main__':
    main()
