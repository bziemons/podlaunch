#!/usr/bin/env python3

import logging
import pathlib
from typing import Set

import click
import sh
from sh import systemctl
from sh import podman

SERVICES_BASE_PATH = "/docker/services/"


def resolve_image_units():
    services_path = pathlib.Path(SERVICES_BASE_PATH)
    services_list = list(map(lambda p: str(p.name), services_path.iterdir()))
    image_units: Set[str] = set()

    def process_pod_systemctl_show(line: str):
        search_str = "Wants="
        if line.startswith(search_str):
            for unit in line[len(search_str) :].split(" "):
                if unit.startswith("image@") and unit.endswith(".service"):
                    image_units.add(unit)

    with click.progressbar(
        length=len(services_list) * 2, label="Collecting container image services.."
    ) as bar:
        started_processes = []
        for service in services_list:
            process = systemctl.show(
                f"pod@{service}.service",
                _out=process_pod_systemctl_show,
                _bg=True,
                _done=lambda cmd, success, exit_code: bar.update(1),
            )
            started_processes.append(process)
        # join processes
        [p.wait() for p in started_processes]

        first = True
        new_image_units: Set[str] = set(image_units)
        previous_image_units: Set[str] = set(image_units)
        bar.length = len(image_units) * 2

        while first or len(new_image_units) > 0:
            first = False
            started_processes = []
            for service in new_image_units:
                process = systemctl.show(
                    service,
                    _out=process_pod_systemctl_show,
                    _bg=True,
                    _done=lambda cmd, success, exit_code: bar.update(1),
                )
                started_processes.append(process)
            # join processes
            [p.wait() for p in started_processes]
            new_image_units = image_units.difference(previous_image_units)
            bar.length += len(new_image_units)
            previous_image_units = set(image_units)

    return image_units


def main():
    logging.basicConfig(level=logging.CRITICAL)
    image_units = resolve_image_units()
    image_tags: Set[str] = set()

    def process_image_systemctl_show(line: str):
        search_str = "Environment="
        if line.startswith(search_str):
            for unit in line[len(search_str) :].split(" "):
                search_str = "IMAGE_TAG="
                if unit.startswith(search_str):
                    image_tags.add(unit[len(search_str) :])

    started_processes = []
    with click.progressbar(image_units, label="Collecting container images..") as bar:
        for image_service in bar:
            process = systemctl.show(
                image_service,
                _out=process_image_systemctl_show,
                _bg=True,
                _done=lambda cmd, success, exit_code: bar.update(1),
            )
            started_processes.append(process)
        # join processes
        [p.wait() for p in started_processes]

    started_processes = []
    with click.progressbar(
        image_tags, label="Untagging container images..", show_pos=True
    ) as bar:
        for image_tag in bar:
            process = podman.untag(
                image_tag,
                _bg=True,
                _done=lambda cmd, success, exit_code: bar.update(1),
            )
            started_processes.append(process)
        # join processes
        for p in started_processes:
            try:
                p.wait()
            except sh.ErrorReturnCode:
                # ignore missing tags
                if "image not known".encode() not in p.stderr:
                    raise

    started_processes = []
    with click.progressbar(
        image_units, label="Building images..", show_pos=True
    ) as bar:
        for image_service in bar:
            process = systemctl.restart(
                image_service,
                _bg=True,
                _done=lambda cmd, success, exit_code: bar.update(1),
            )
            started_processes.append(process)
        # join processes
        [p.wait() for p in started_processes]


if __name__ == "__main__":
    main()
