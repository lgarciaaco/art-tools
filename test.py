import asyncio
import logging
import os

from doozerlib import brew
from doozerlib.backend.konflux_image_builder import KonfluxImageBuilder


def build_retrying_koji_client():
    """
    :return: Returns a new koji client instance that will automatically retry
    methods when it receives common exceptions (e.g. Connection Reset)
    Honors doozer --brew-event.
    """
    return brew.KojiWrapper(['https://brewhub.engineering.redhat.com/brewhub'])


async def main():
    image_repo_creds = {
        "username": os.environ.get("KONFLUX_ART_IMAGES_USERNAME"),
        "password": os.environ.get("KONFLUX_ART_IMAGES_PASSWORD")
    }
    installed_packages = await KonfluxImageBuilder.get_installed_packages(
        'quay.io/redhat-user-workloads/ocp-art-tenant/art-images:ose-csi-driver-nfs-rhel9-v4.20.0-20250422.172925',
        ['amd64'],
        image_repo_creds,
        logging.getLogger())
    print(installed_packages)


if __name__ == '__main__':
    asyncio.run(main())