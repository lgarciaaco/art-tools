import asyncio
import logging
import traceback
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union, cast

import click
from artcommonlib.konflux.konflux_build_record import (
    ArtifactType, Engine, KonfluxBuildRecord, KonfluxBundleBuildRecord)
from artcommonlib.konflux.konflux_db import KonfluxDb
from artcommonlib.model import Missing
from artcommonlib.telemetry import start_as_current_span_async
from doozerlib import constants
from doozerlib.backend.konflux_image_builder import (KonfluxImageBuilder,
                                                     KonfluxImageBuilderConfig)
from doozerlib.backend.konflux_olm_bundler import (KonfluxOlmBundleBuilder,
                                                   KonfluxOlmBundleRebaser)
from doozerlib.backend.rebaser import KonfluxRebaser
from doozerlib.cli import (cli, click_coroutine, option_commit_message,
                           option_push, pass_runtime,
                           validate_semver_major_minor_patch)
from doozerlib.exceptions import DoozerFatalError
from doozerlib.image import ImageMetadata
from doozerlib.runtime import Runtime
from opentelemetry import trace

TRACER = trace.get_tracer(__name__)
LOGGER = logging.getLogger(__name__)


class KonfluxRebaseCli:
    def __init__(
            self,
            runtime: Runtime,
            version: str,
            release: str,
            embargoed: bool,
            force_yum_updates: bool,
            repo_type: str,
            image_repo: str,
            message: str,
            push: bool):
        self.runtime = runtime
        self.version = version
        self.release = release
        self.embargoed = embargoed
        self.force_yum_updates = force_yum_updates
        if repo_type not in ['signed', 'unsigned']:
            raise click.BadParameter(f"repo_type must be one of 'signed' or 'unsigned'. Got: {repo_type}")
        self.repo_type = repo_type
        self.image_repo = image_repo
        self.message = message
        self.push = push
        self.upcycle = runtime.upcycle

    @start_as_current_span_async(TRACER, "beta:images:konflux:rebase")
    async def run(self):
        runtime = self.runtime
        runtime.initialize(mode='images', clone_distgits=False)
        assert runtime.source_resolver is not None, "source_resolver is required for this command"
        metas = runtime.ordered_image_metas()
        base_dir = Path(runtime.working_dir, constants.WORKING_SUBDIR_KONFLUX_BUILD_SOURCES)
        rebaser = KonfluxRebaser(
            runtime=runtime,
            base_dir=base_dir,
            source_resolver=runtime.source_resolver,
            repo_type=self.repo_type,
            upcycle=self.upcycle,
            force_private_bit=self.embargoed,
        )
        tasks = []
        for image_meta in metas:
            tasks.append(asyncio.create_task(rebaser.rebase_to(
                image_meta,
                self.version,
                self.release,
                force_yum_updates=self.force_yum_updates,
                image_repo=self.image_repo,
                commit_message=self.message,
                push=self.push)))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        failed_images = []
        for index, result in enumerate(results):
            if isinstance(result, Exception):
                image_name = metas[index].distgit_key
                failed_images.append(image_name)
                LOGGER.error(f"Failed to rebase {image_name}: {result}")
        if failed_images:
            runtime.state['images:konflux:rebase'] = {'failed-images': failed_images}
            raise DoozerFatalError(f"Failed to rebase images: {failed_images}")
        LOGGER.info("Rebase complete")


@cli.command("beta:images:konflux:rebase", short_help="Refresh a group's konflux source content from source content.")
@click.option("--version", metavar='VERSION', required=True, callback=validate_semver_major_minor_patch,
              help="Version string to populate in Dockerfiles. \"auto\" gets version from atomic-openshift RPM")
@click.option("--release", metavar='RELEASE', required=True, help="Release string to populate in Dockerfiles.")
@click.option("--embargoed", is_flag=True, help="Add .p1 to the release string for all images, which indicates those images have embargoed fixes")
@click.option("--force-yum-updates", is_flag=True, default=False,
              help="Inject \"yum update -y\" in the final stage of an image build. This ensures the component image will be able to override RPMs it is inheriting from its parent image using RPMs in the rebuild plashet.")
@click.option("--repo-type", metavar="REPO_TYPE", envvar="OIT_IMAGES_REPO_TYPE",
              default="unsigned",
              help="Repo group type to use (e.g. signed, unsigned).")
@click.option('--image-repo', default=constants.KONFLUX_DEFAULT_IMAGE_REPO, help='Image repo for base images')
@option_commit_message
@option_push
@pass_runtime
@click_coroutine
async def images_konflux_rebase(runtime: Runtime, version: str, release: str, embargoed: bool, force_yum_updates: bool,
                                repo_type: str, image_repo: str, message: str, push: bool):
    """
    Refresh a group's konflux content from source content.
    """
    cli = KonfluxRebaseCli(
        runtime=runtime,
        version=version,
        release=release,
        embargoed=embargoed,
        force_yum_updates=force_yum_updates,
        repo_type=repo_type,
        image_repo=image_repo,
        message=message,
        push=push,
    )
    await cli.run()


class KonfluxBuildCli:
    def __init__(
        self,
        runtime: Runtime,
        konflux_kubeconfig: Optional[str],
        konflux_context: Optional[str],
        konflux_namespace: str,
        image_repo: str,
        skip_checks: bool,
        dry_run: bool,
        plr_template,
    ):
        self.runtime = runtime
        self.konflux_kubeconfig = konflux_kubeconfig
        self.konflux_context = konflux_context
        self.konflux_namespace = konflux_namespace
        self.image_repo = image_repo
        self.skip_checks = skip_checks
        self.dry_run = dry_run
        self.plr_template = plr_template

    @start_as_current_span_async(TRACER, "images:konflux:build")
    async def run(self):
        runtime = self.runtime
        runtime.initialize(mode='images', clone_distgits=False)
        runtime.konflux_db.bind(KonfluxBuildRecord)
        assert runtime.source_resolver is not None, "source_resolver is not initialized. Doozer bug?"
        metas = runtime.ordered_image_metas()
        config = KonfluxImageBuilderConfig(
            base_dir=Path(runtime.working_dir, constants.WORKING_SUBDIR_KONFLUX_BUILD_SOURCES),
            group_name=runtime.group,
            kubeconfig=self.konflux_kubeconfig,
            context=self.konflux_context,
            namespace=self.konflux_namespace,
            image_repo=self.image_repo,
            skip_checks=self.skip_checks,
            dry_run=self.dry_run,
            plr_template=self.plr_template
        )
        builder = KonfluxImageBuilder(config=config)
        tasks = []
        for image_meta in metas:
            tasks.append(asyncio.create_task(builder.build(image_meta)))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        failed_images = []
        for index, result in enumerate(results):
            if isinstance(result, Exception):
                image_name = metas[index].distgit_key
                failed_images.append(image_name)
                stack_trace = ''.join(traceback.TracebackException.from_exception(result).format())
                LOGGER.error(f"Failed to build {image_name}: {result}; {stack_trace}")
        if failed_images:
            raise DoozerFatalError(f"Failed to build images: {failed_images}")
        LOGGER.info("Build complete")


@cli.command("beta:images:konflux:build", short_help="Build images for the group.")
@click.option('--konflux-kubeconfig', metavar='PATH', help='Path to the kubeconfig file to use for Konflux cluster connections.')
@click.option('--konflux-context', metavar='CONTEXT', help='The name of the kubeconfig context to use for Konflux cluster connections.')
@click.option('--konflux-namespace', metavar='NAMESPACE', required=True, help='The namespace to use for Konflux cluster connections.')
@click.option('--image-repo', default=constants.KONFLUX_DEFAULT_IMAGE_REPO, help='Push images to the specified repo.')
@click.option('--skip-checks', default=False, is_flag=True, help='Skip all post build checks')
@click.option('--dry-run', default=False, is_flag=True, help='Do not build anything, but only print build operations.')
@click.option('--plr-template', required=False, default='',
              help='Override the Pipeline Run template commit from openshift-priv/art-konflux-template')
@pass_runtime
@click_coroutine
async def images_konflux_build(
        runtime: Runtime, konflux_kubeconfig: Optional[str], konflux_context: Optional[str],
        konflux_namespace: str, image_repo: str, skip_checks: bool, dry_run: bool, plr_template):
    cli = KonfluxBuildCli(
        runtime=runtime, konflux_kubeconfig=konflux_kubeconfig,
        konflux_context=konflux_context, konflux_namespace=konflux_namespace,
        image_repo=image_repo, skip_checks=skip_checks, dry_run=dry_run, plr_template=plr_template)
    await cli.run()


class KonfluxBundleCli:
    def __init__(
        self,
        runtime: Runtime,
        operator_nvrs: Sequence[str],
        force: bool,
        dry_run: bool,
        konflux_kubeconfig: Optional[str],
        konflux_context: Optional[str],
        konflux_namespace: str,
        image_repo: str,
        skip_checks: bool,
        release: Optional[str],
    ):
        self.runtime = runtime
        self.operator_nvrs = list(operator_nvrs)
        self.force = force
        self.dry_run = dry_run
        self.konflux_kubeconfig = konflux_kubeconfig
        self.konflux_context = konflux_context
        self.konflux_namespace = konflux_namespace
        self.image_repo = image_repo
        self.skip_checks = skip_checks
        self.release = release

    async def get_operator_builds(self):
        """ Get build records for the given operator nvrs or latest build records for all operators.

        :return: A dictionary of operator name to build records.
        """
        runtime = self.runtime
        assert runtime.konflux_db is not None, "konflux_db is not initialized. Doozer bug?"
        assert runtime.assembly is not None, "assembly is not initialized. Doozer bug?"
        dgk_records: Dict[str, KonfluxBuildRecord] = {}  # operator name to build records
        if self.operator_nvrs:
            # Get build records for the given operator nvrs
            LOGGER.info("Fetching given nvrs from Konflux DB...")
            records = await runtime.konflux_db.get_build_records_by_nvrs(self.operator_nvrs)
            for record in records:
                assert record is not None and isinstance(record, KonfluxBuildRecord), "Invalid record. Doozer bug?"
                dgk_records[record.name] = record
            # Load image metas for the given operators
            runtime.images = list(dgk_records.keys())
            runtime.initialize(mode='images', clone_distgits=False)
            for dkg in dgk_records.keys():
                metadata = runtime.image_map[dkg]
                if not metadata.is_olm_operator:
                    raise DoozerFatalError(f"Operator {dkg} does not have 'update-csv' config")
        else:
            # Get latest build records for all specified operators
            runtime.initialize(mode='images', clone_distgits=False)
            LOGGER.info("Fetching latest operator builds from Konflux DB...")
            operator_metas: List[ImageMetadata] = [operator_meta for operator_meta in runtime.ordered_image_metas() if operator_meta.is_olm_operator]
            records = await runtime.konflux_db.get_latest_builds(
                names=[metadata.distgit_key for metadata in operator_metas],
                group=runtime.group,
                assembly=runtime.assembly,
                artifact_type=ArtifactType.IMAGE,
                engine=Engine.KONFLUX,
                strict=True,
            )
            for metadata, record in zip(operator_metas, records):
                assert record is not None and isinstance(record, KonfluxBuildRecord)
                dgk_records[metadata.distgit_key] = record
        return dgk_records

    async def _rebase_and_build(
            self,
            rebaser: KonfluxOlmBundleRebaser,
            builder: KonfluxOlmBundleBuilder,
            db_for_bundles: KonfluxDb,
            image_meta: ImageMetadata,
            operator_build: KonfluxBuildRecord):
        logger = LOGGER.getChild(f"[{image_meta.distgit_key}]")
        runtime = self.runtime
        assembly = runtime.assembly
        input_release = self.release
        if not self.force or not input_release:
            logger.info("Checking if a previous bundle build exists...")
            bundle_build = await db_for_bundles.get_latest_build(
                name=image_meta.get_olm_bundle_short_name(),
                group=runtime.group,
                assembly=assembly,
                engine=Engine.KONFLUX,
                strict=False,
            )
            if bundle_build is not None:
                logger.info(f"A previous bundle build already exists: {bundle_build.nvr}")
                if not self.force:
                    logger.info("Skipping because --force is not set")
                    return
                input_release = str(int(bundle_build.release) + 1)
                logger.info("Force rebuild requested because --force is set; release string will be %s", input_release)
            else:
                input_release = "1"
                logger.info("No previous bundle build found; a new bundle build will be created with release string %s", input_release)

        logger.info("Rebasing OLM bundle...")
        await rebaser.rebase(image_meta, operator_build, input_release)
        logger.info("Building OLM bundle...")
        await builder.build(image_meta)
        logger.info("Bundle build complete")

    @start_as_current_span_async(TRACER, "images:konflux:bundle")
    async def run(self):
        runtime = self.runtime
        if runtime.images and self.operator_nvrs:
            raise click.BadParameter("Do not specify operator NVRs when --images is specified")

        runtime.initialize(config_only=True)
        assembly = runtime.assembly
        if assembly is None:
            raise ValueError("Assemblies feature is disabled for this group. This is no longer supported.")
        assert runtime.konflux_db is not None, "konflux_db is not initialized. Doozer bug?"
        konflux_db = runtime.konflux_db
        konflux_db.bind(KonfluxBuildRecord)

        dgk_records = await self.get_operator_builds()

        assert runtime.source_resolver is not None, "source_resolver is not initialized. Doozer bug?"
        assert runtime.group_config is not None, "group_config is not initialized. Doozer bug?"

        rebaser = KonfluxOlmBundleRebaser(
            base_dir=Path(runtime.working_dir, constants.WORKING_SUBDIR_KONFLUX_BUILD_SOURCES),
            group=runtime.group,
            assembly=assembly,
            group_config=runtime.group_config,
            konflux_db=runtime.konflux_db,
            source_resolver=runtime.source_resolver,
            upcycle=runtime.upcycle,
            image_repo=self.image_repo,
            dry_run=self.dry_run,
        )
        db_for_bundles = KonfluxDb()
        db_for_bundles.bind(KonfluxBundleBuildRecord)
        builder = KonfluxOlmBundleBuilder(
            base_dir=Path(runtime.working_dir, constants.WORKING_SUBDIR_KONFLUX_BUILD_SOURCES),
            group=runtime.group,
            assembly=assembly,
            source_resolver=runtime.source_resolver,
            db=db_for_bundles,
            konflux_namespace=self.konflux_namespace,
            konflux_kubeconfig=self.konflux_kubeconfig,
            konflux_context=self.konflux_context,
            image_repo=self.image_repo,
            skip_checks=self.skip_checks,
            dry_run=self.dry_run,
        )

        tasks = []
        for dkg, record in dgk_records.items():
            image_meta = runtime.image_map[dkg]
            tasks.append(asyncio.create_task(self._rebase_and_build(rebaser, builder, db_for_bundles, image_meta, record)))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        failed_tasks = []
        for dgk, result in zip(dgk_records, results):
            if isinstance(result, Exception):
                failed_tasks.append(dgk)
                stack_trace = ''.join(traceback.TracebackException.from_exception(result).format())
                LOGGER.error(f"Failed to rebase/build OLM bundle for {dgk}: {result}; {stack_trace}")
        if failed_tasks:
            raise DoozerFatalError(f"Failed to rebase/build bundles: {failed_tasks}")
        LOGGER.info("Build complete")


@cli.command("beta:images:konflux:bundle", short_help="Rebase and build an OLM bundle for an operator with Konflux.")
@click.argument('operator_nvrs', nargs=-1, required=False)
@click.option("-f", "--force", required=False, is_flag=True,
              help="Perform a build even if previous bundles for given NVRs already exist")
@click.option('--dry-run', default=False, is_flag=True,
              help='Do not push to build repo or build anything, but print what would be done.')
@click.option('--konflux-kubeconfig', metavar='PATH', help='Path to the kubeconfig file to use for Konflux cluster connections.')
@click.option('--konflux-context', metavar='CONTEXT', help='The name of the kubeconfig context to use for Konflux cluster connections.')
@click.option('--konflux-namespace', metavar='NAMESPACE', required=True, help='The namespace to use for Konflux cluster connections.')
@click.option('--image-repo', default=constants.KONFLUX_DEFAULT_IMAGE_REPO, help='Push images to the specified repo.')
@click.option('--skip-checks', default=False, is_flag=True, help='Skip all post build checks')
@click.option("--release", metavar='RELEASE', help="Release string to populate in bundle's Dockerfiles.")
@pass_runtime
@click_coroutine
async def images_konflux_bundle(
        runtime: Runtime, operator_nvrs: Tuple[str, ...], force: bool, dry_run: bool,
        konflux_kubeconfig: Optional[str], konflux_context: Optional[str],
        konflux_namespace: str, image_repo: str, skip_checks: bool, release: Optional[str]):
    cli = KonfluxBundleCli(
        runtime=runtime, operator_nvrs=operator_nvrs, force=force, dry_run=dry_run,
        konflux_kubeconfig=konflux_kubeconfig, konflux_context=konflux_context,
        konflux_namespace=konflux_namespace, image_repo=image_repo, skip_checks=skip_checks,
        release=release)
    await cli.run()
