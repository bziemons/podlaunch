#!/usr/bin/env python3

import logging
import multiprocessing
import pathlib
import time
from typing import Set, Callable

import click
import sh
from sh import podman
from sh import systemctl

SERVICES_BASE_PATH = "/docker/services/"


def resolve_image_units():
    services_path = pathlib.Path(SERVICES_BASE_PATH)
    services_set = set(map(lambda p: str(p.name), services_path.iterdir()))

    logging.info(f"Found {len(services_set)} services: {str(services_set)}")

    systemctl("daemon-reload")

    def remove_masked_unit(
        _item_set: Set[str],
        item: str,
        item_to_unit: Callable[[str], str] = lambda i: i,
    ):
        load_state = systemctl.show(
            "--property=LoadState", "--value", item_to_unit(item)
        )
        load_state = load_state.stdout.strip().decode(
            encoding="utf-8", errors="replace"
        )
        logging.debug(f"{item} load state: {repr(load_state)}")
        if load_state == "masked":
            logging.info(f"Removed masked entry: {item}")
            _item_set.remove(item)

    with click.progressbar(list(services_set), label="Checking service units..", show_pos=True) as bar:
        for service in bar:
            remove_masked_unit(services_set, service, lambda srv: f"pod@{srv}.service")

    def add_wants_to_image_units(_image_units: Set[str], unit: str):
        wants = systemctl.show("--property=Wants", "--value", unit)
        wants_list = (
            wants.stdout.strip().decode(encoding="utf-8", errors="replace").split(" ")
        )
        logging.debug(f"{unit} wants: {repr(wants_list)}")
        for next_unit in wants_list:
            if next_unit.startswith("image@") and next_unit.endswith(".service"):
                logging.info(f"Found {unit} wants {next_unit}")
                _image_units.add(next_unit)

    image_units: Set[str] = set()

    with click.progressbar(
        length=len(services_set) * 2, label="Collecting container image services.."
    ) as bar:
        for service in services_set:
            add_wants_to_image_units(image_units, f"pod@{service}.service")
            bar.update(1)

        new_image_units: Set[str] = set(image_units)
        bar.length = len(image_units) * 2

        while len(new_image_units) > 0:
            units_to_check = list(new_image_units)
            new_image_units = set()  # reset new image units
            for image_unit in units_to_check:
                add_wants_to_image_units(new_image_units, image_unit)
                bar.update(1)

            bar.length += len(new_image_units)
            image_units.update(
                new_image_units
            )  # add new image units to all image units

    with click.progressbar(
        list(image_units), label="Checking container image units..", show_pos=True
    ) as bar:
        for image_unit in bar:
            remove_masked_unit(image_units, image_unit)

    logging.info(f"Found {len(image_units)} images: {str(image_units)}")
    return image_units


@click.command()
@click.option("--verbose", is_flag=True, default=False, help="Enable INFO logging")
def main(verbose):
    if verbose:
        loglevel = logging.INFO
    else:
        loglevel = logging.CRITICAL

    logging.basicConfig(level=loglevel)
    image_units = resolve_image_units()
    image_tags: Set[str] = set()

    with click.progressbar(image_units, label="Collecting container image tags..") as bar:
        for image_unit in bar:
            environment = systemctl.show(
                "--property=Environment",
                "--value",
                image_unit,
            )
            environment_list = (
                environment.stdout.strip()
                .decode(encoding="utf-8", errors="replace")
                .split(" ")
            )
            logging.debug(f"{image_unit} environment: {repr(environment_list)}")
            for envvar in environment_list:
                search_str = "IMAGE_TAG="
                if envvar.startswith(search_str):
                    image_tags.add(envvar[len(search_str) :])

    started_processes = []
    with click.progressbar(
        length=len(image_tags), label="Untagging container images..", show_pos=True
    ) as bar:
        for image_tag in image_tags:
            process = podman.untag(
                image_tag,
                _bg=True,
                _err_to_out=True,
                _done=lambda cmd, success, exit_code: bar.update(1),
            )
            started_processes.append(process)
            time.sleep(0.02)
        # join processes
        for p in started_processes:
            try:
                p.wait()
            except sh.ErrorReturnCode as error:
                # ignore missing image tags
                if "image not known".encode() in p.stdout:
                    pass
                else:
                    raise

    started_processes = []
    with click.progressbar(
        length=len(image_units), label="Building images..", show_pos=True
    ) as bar:
        semaphore = multiprocessing.Semaphore(8)
        for image_unit in image_units:
            try:
                systemctl("reset-failed", image_unit, _bg=False, _err_to_out=True)
            except sh.ErrorReturnCode as error:
                if f"Unit {image_unit} not loaded".encode() in error.stdout:
                    logging.info(
                        f"Not resetting failed state for {image_unit}, unit not loaded"
                    )
                else:
                    raise

            semaphore.acquire()

            def restart_done(cmd, success, exit_code):
                bar.update(1)
                if not success:
                    logging.warning(f"{cmd.cmd}{tuple(cmd.call_args)} completed with exit code {exit_code}")
                semaphore.release()

            process = systemctl.restart(image_unit, _bg=True, _done=restart_done)
            started_processes.append(process)
        # join processes
        [p.wait() for p in started_processes]


if __name__ == "__main__":
    main()
