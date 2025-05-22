from collections.abc import Iterable
import datetime
import functools
import json
import os
import pathlib
import sys
import typing
import uuid # For generating doc_ids

import click

from docstore.storage import Storage, LocalStorage, S3Storage
from docstore import documents as documents_api # To avoid conflict with documents variable


@click.group()
@click.option(
    "--root",
    default=".",
    help="The root of the docstore database (used by LocalStorage if DOCSTORE_ROOT_PATH is not set).",
    type=click.Path(),
    show_default=True,
)
@click.pass_context
def main(ctx, root):  # type: ignore
    # ctx.obj will store the --root CLI option, primarily for LocalStorage fallback
    # and for commands not yet fully refactored.
    ctx.obj = {"cli_root": pathlib.Path(root)}


def _get_storage_instance(ctx: click.Context, cli_root_path: pathlib.Path) -> Storage:
    """
    Determines the storage backend from environment variables and CLI args,
    then instantiates and returns the appropriate storage object.
    """
    backend = os.environ.get("DOCSTORE_STORAGE_BACKEND", "local").lower()

    if backend == "s3":
        bucket_name = os.environ.get("DOCSTORE_S3_BUCKET_NAME")
        aws_access_key_id = os.environ.get("AWS_ACCESS_KEY_ID")
        aws_secret_access_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
        # Use AWS_DEFAULT_REGION if DOCSTORE_S3_REGION_NAME is not set
        region_name = os.environ.get("DOCSTORE_S3_REGION_NAME", os.environ.get("AWS_DEFAULT_REGION"))
        endpoint_url = os.environ.get("DOCSTORE_S3_ENDPOINT_URL") # Optional

        if not all([bucket_name, aws_access_key_id, aws_secret_access_key, region_name]):
            missing = []
            if not bucket_name: missing.append("DOCSTORE_S3_BUCKET_NAME")
            if not aws_access_key_id: missing.append("AWS_ACCESS_KEY_ID")
            if not aws_secret_access_key: missing.append("AWS_SECRET_ACCESS_KEY")
            if not region_name: missing.append("DOCSTORE_S3_REGION_NAME or AWS_DEFAULT_REGION")
            raise click.UsageError(
                f"S3 backend is selected, but required environment variable(s) are missing: {', '.join(missing)}"
            )
        
        click.echo(f"Using S3 storage backend (bucket: {bucket_name}, region: {region_name}).")
        return S3Storage(
            bucket_name=bucket_name,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            region_name=region_name,
            endpoint_url=endpoint_url
        )
    elif backend == "local":
        # Prioritize DOCSTORE_ROOT_PATH env var, then CLI --root, then default '.'
        root_path_str = os.environ.get("DOCSTORE_ROOT_PATH", str(cli_root_path))
        actual_root_path = pathlib.Path(root_path_str).resolve()
        click.echo(f"Using local storage backend (root: {actual_root_path}).")
        return LocalStorage(root_path=actual_root_path)
    else:
        raise click.UsageError(
            f"Invalid DOCSTORE_STORAGE_BACKEND: '{backend}'. Must be 'local' or 's3'."
        )


def _require_existing_instance(inner):  # type: ignore
    """
    When you call ``docstore add``, most of the time you want to be adding
    documents to an existing instance, not creating a new instance.

    It's easy to get the directory wrong, so this decorator will check you
    really wanted to create a new instance vs. adding to an old one.
    """
    # TODO: This decorator relies on db_path and local filesystem checks.
    # It will need refactoring when `add` command fully supports different storage backends.
    # For now, it will only work correctly if LocalStorage is implicitly or explicitly used
    # and the --root path points to a valid local setup.
    @functools.wraps(inner)
    def wrapper(*args, **kwargs):  # type: ignore
        from docstore.documents import db_path # db_path is problematic for non-local storage

        ctx_obj = click.get_current_context().obj
        # Assuming cli_root is available in ctx.obj for this check
        cli_root = ctx_obj.get("cli_root", pathlib.Path("."))


        # This check is problematic for S3 backend.
        # For now, we might bypass it if S3 is explicitly configured,
        # or accept it won't work correctly for S3 yet.
        backend_env = os.environ.get("DOCSTORE_STORAGE_BACKEND", "local").lower()

        if backend_env == "local":
            if (
                cli_root == pathlib.Path(".") # Check if it's the default path
                and not os.path.exists(db_path(cli_root)) # db_path expects a pathlib.Path
                and not any(ag == "--root" or ag.startswith("--root=") for ag in sys.argv if ag !=".")
            ):  # pragma: no cover
                click.echo(
                    f"There is no existing local docstore instance at {os.path.abspath(str(cli_root))}",
                    err=True,
                )
                click.confirm("Do you want to create a new local instance?", abort=True, err=True)

        return inner(*args, **kwargs)

    return wrapper


@main.command(help="Run a docstore API server")
@click.option(
    "--host", default="127.0.0.1", help="The interface to bind to.", show_default=True
)
@click.option("--port", default=3391, help="The port to bind to.", show_default=True)
@click.option("--title", default="", help="The title of the app.")
@click.option(
    "--thumbnail_width", default=200, help="Thumbnail width (px).", show_default=True
)
@click.option("--debug", default=False, is_flag=True, help="Run in debug mode.")
@click.option("--profile", default=False, is_flag=True, help="Run a profiler.")
@click.pass_context # Changed from pass_obj to pass_context to access full context
def serve(
    ctx: click.Context, # Use ctx to get cli_root from obj
    host: str,
    port: int,
    debug: bool,
    profile: bool,
    title: str,
    thumbnail_width: int,
) -> None:  # pragma: no cover
    from docstore.server import create_app, run_profiler, run_server

    cli_root_path = ctx.obj["cli_root"]
    storage_instance = _get_storage_instance(ctx, cli_root_path)
    
    # Note: create_app now expects 'storage' instead of 'root'
    app = create_app(storage=storage_instance, title=title, thumbnail_width=thumbnail_width)

    if profile:
        run_profiler(app, host=host, port=port)
    else:
        run_server(app, host=host, port=port, debug=debug)


def _add_document(
    storage: Storage,
    original_file_path: pathlib.Path,
    title_str: str | None,
    tags_str: str | None,
    source_url_str: str | None,
) -> None:
    """
    Helper function to add a document to the store using the provided storage backend.
    """
    try:
        file_content_bytes = original_file_path.read_bytes()
    except Exception as e:
        raise click.ClickException(f"Error reading file {original_file_path}: {e}")

    doc_id = str(uuid.uuid4())
    original_filename = original_file_path.name
    
    processed_tags = [t.strip() for t in (tags_str or "").split(",") if t.strip()]

    document = documents_api.store_new_document(
        storage=storage,
        doc_id=doc_id,
        original_filename=original_filename,
        file_content_bytes=file_content_bytes,
        title=title_str or "",
        tags=processed_tags,
        source_url=source_url_str,
        date_saved=datetime.datetime.now(),
    )
    click.echo(document.id)


@main.command(help="Store a file in docstore")
@click.argument("path_arg", nargs=1, type=click.Path(exists=True, dir_okay=False), required=True)
@click.option(
    "--title",
    help="The title of the file.",
    required=True,
    prompt="What is the title of the file?",
)
@click.option(
    "--tags",
    help="The tags to apply to the file.",
    required=True,
    prompt="How should the file be tagged?",
)
@click.option("--source_url", help="Where was this file downloaded from?.")
@click.pass_context
@_require_existing_instance
def add(ctx: click.Context, path_arg: str, title: str, tags: str, source_url: str | None) -> None:
    cli_root_path = ctx.obj["cli_root"]
    storage_instance = _get_storage_instance(ctx, cli_root_path)
    
    original_file_path = pathlib.Path(path_arg)

    _add_document(
        storage=storage_instance,
        original_file_path=original_file_path,
        title_str=title,
        tags_str=tags,
        source_url_str=source_url
    )


@main.command(help="Store a file on the web in docstore")
@click.option(
    "--url", help="URL of the file to store.", type=str, required=True
)
@click.option("--title", help="The title of the file.")
@click.option("--tags", help="The tags to apply to the file.")
@click.option("--source_url", help="Where was this file downloaded from?.")
@click.pass_context
@_require_existing_instance
def add_from_url(
    ctx: click.Context,
    url: str,
    title: str | None,
    tags: str | None,
    source_url: str | None,
) -> None:  # pragma: no cover
    from docstore.downloads import download_file
    cli_root_path = ctx.obj["cli_root"]
    storage_instance = _get_storage_instance(ctx, cli_root_path)

    click.echo(f"Downloading {url}...")
    downloaded_file_path = None
    try:
        downloaded_file_path = download_file(url) # Returns a pathlib.Path
        click.echo(f"Downloaded to temporary file: {downloaded_file_path}")

        _add_document(
            storage=storage_instance,
            original_file_path=downloaded_file_path,
            title_str=title,
            tags_str=tags,
            source_url_str=source_url or url # Default source_url to the download URL
        )
    except Exception as e:
        click.echo(f"Error during add_from_url: {e}", err=True)
        # Potentially re-raise or handle more gracefully
    finally:
        if downloaded_file_path and os.path.exists(downloaded_file_path):
            try:
                os.unlink(downloaded_file_path)
                click.echo(f"Cleaned up temporary file: {downloaded_file_path}")
            except Exception as e:
                click.echo(f"Error cleaning up temporary file {downloaded_file_path}: {e}", err=True)


@main.command(help="Migrate a V1 docstore")
@click.option(
    "--v1_path",
    help="Path to the root of the V1 instance.",
    type=click.Path(exists=True, file_okay=False, dir_okay=True), # Ensure it's a dir
    required=True,
)
@click.pass_context # Changed to pass_context
def migrate(ctx: click.Context, v1_path: str) -> None:  # pragma: no cover
    cli_root_path = ctx.obj["cli_root"]
    storage_instance = _get_storage_instance(ctx, cli_root_path) # Get storage for potential future use
    v1_actual_path = pathlib.Path(v1_path)

    # This command heavily relies on local FS operations and the old store_new_document signature.
    # It is NOT storage-agnostic.
    if not isinstance(storage_instance, LocalStorage):
        click.echo(
            "Error: Migrate command currently only supports migrating TO a LocalStorage backend.",
            err=True
        )
        click.echo(
            "Please ensure DOCSTORE_STORAGE_BACKEND is 'local' or not set, and DOCSTORE_ROOT_PATH or --root points to your target local docstore.",
            err=True
        )
        sys.exit(1)
    
    click.echo(f"Migrating V1 data from {v1_actual_path} to LocalStorage at {cli_root_path}...")

    v1_documents_json_path = v1_actual_path / "documents.json"
    if not v1_documents_json_path.exists():
        click.echo(f"Error: V1 documents.json not found at {v1_documents_json_path}", err=True)
        sys.exit(1)

    v1_docs_data = json.loads(v1_documents_json_path.read_text())

    for doc_id_v1, doc_v1 in v1_docs_data.items():
        original_filename_v1 = doc_v1.get("filename", doc_v1.get("file_identifier"))
        if not original_filename_v1:
            click.echo(f"Skipping doc ID {doc_id_v1} due to missing filename/identifier.", err=True)
            continue
            
        v1_file_on_disk = v1_actual_path / "files" / doc_v1["file_identifier"]

        if v1_file_on_disk.exists():
            try:
                file_content_bytes = v1_file_on_disk.read_bytes()
                new_doc_id = str(uuid.uuid4()) # Generate a new ID for V2

                # Call the refactored store_new_document
                documents_api.store_new_document(
                    storage=storage_instance, # This is confirmed LocalStorage
                    doc_id=new_doc_id,
                    original_filename=original_filename_v1,
                    file_content_bytes=file_content_bytes,
                    title=doc_v1.get("title", ""),
                    tags=doc_v1.get("tags", []),
                    source_url=doc_v1.get("user_data", {}).get("source_url", ""),
                    date_saved=datetime.datetime.fromisoformat(doc_v1["date_created"]),
                )
                click.echo(f"Migrated: {original_filename_v1} (V1 ID: {doc_id_v1}) -> New ID: {new_doc_id}")
            except Exception as e:
                click.echo(f"Error migrating doc {original_filename_v1} (V1 ID: {doc_id_v1}): {e}", err=True)
        else:
            click.echo(f"Warning: V1 file {v1_file_on_disk} not found for doc ID {doc_id_v1}", err=True)
                title=doc.get("title", ""),
                tags=doc.get("tags", []),
                source_url=doc.get("user_data", {}).get("source_url", ""),
                date_saved=datetime.datetime.fromisoformat(doc["date_created"]),
            )
            print(doc.get("filename", os.path.basename(doc["file_identifier"])))
        else:
            click.echo(f"Warning: File {stored_file_path} not found for doc ID {doc.get('id', 'N/A')}", err=True)


@main.command(help="Delete one or more documents")
@click.argument("doc_ids", nargs=-1)
@click.pass_context
def delete(ctx: click.Context, doc_ids: list[str]) -> None:
    cli_root_path = ctx.obj["cli_root"]
    storage_instance = _get_storage_instance(ctx, cli_root_path)
    
    if not doc_ids:
        click.echo("No document IDs provided for deletion.", err=True)
        return

    for d_id in doc_ids:
        try:
            documents_api.delete_document(storage=storage_instance, doc_id=d_id)
            click.echo(f"Document {d_id} and its associated files marked for deletion/deleted.")
        except ValueError as e: # If delete_document raises ValueError for not found
            click.echo(f"Error deleting document {d_id}: {e}", err=True)
        except Exception as e:
            click.echo(f"An unexpected error occurred while deleting document {d_id}: {e}", err=True)


@main.command(help="Verify your stored files")
@click.pass_context
def verify(ctx: click.Context) -> None:
    import collections
    # sha256 (local path) is no longer used here, sha256_bytes from documents.py is used
    from docstore.documents import read_documents as api_read_documents, sha256_bytes
    import tqdm

    cli_root_path = ctx.obj["cli_root"]
    storage_instance = _get_storage_instance(ctx, cli_root_path)
    
    errors = collections.defaultdict(list)
    
    documents_to_verify = api_read_documents(storage_instance)

    if not documents_to_verify:
        click.echo("No documents found to verify.")
        return

    for doc in tqdm.tqdm(list(documents_to_verify)):
        for f_idx, f in enumerate(doc.files):
            file_description = f"Doc ID: {doc.id}, File: {f.filename} (stored: {f.path})"
            try:
                file_bytes = storage_instance.get_document_file(f.path)
                
                # Verify size
                actual_size = len(file_bytes)
                if f.size != actual_size:
                    errors[doc.id].append(
                        f"Size mismatch for {file_description}\n  actual   = {actual_size}\n  expected = {f.size}"
                    )

                # Verify checksum
                actual_checksum = sha256_bytes(file_bytes)
                if f.checksum != actual_checksum:
                    errors[doc.id].append(
                        f"Checksum mismatch for {file_description}\n  actual   = {actual_checksum}\n  expected = {f.checksum}"
                    )
            except FileNotFoundError:
                 errors[doc.id].append(f"File not found in storage: {file_description}")
            except Exception as e:
                errors[doc.id].append(f"Error verifying {file_description}: {e}")


    from pprint import pprint

    if errors:
        click.echo("Verification found errors:", err=True)
        pprint(errors)
    else:
        click.echo("Verification complete. No errors found.")


@main.command(help="Merge the files on two documents")
@click.argument("doc_ids", nargs=-1)
@click.option("--yes", is_flag=True, help="Skip confirmation prompts.")
@click.pass_context # Changed to pass_context
def merge(ctx: click.Context, doc_ids: list[str], yes: bool) -> None:
    if len(doc_ids) == 1:
        return

    cli_root_path = ctx.obj["cli_root"]
    storage_instance = _get_storage_instance(ctx, cli_root_path)
    
    # pairwise_merge_documents now expects storage and doc IDs.
    
    # Fetching all documents first to validate IDs and get initial data for prompts.
    # This is kept similar to old logic, but uses the new read_documents.
    all_docs_map = {d.id: d for d in documents_api.read_documents(storage_instance)}

    if not doc_ids or len(doc_ids) < 2:
        click.echo("Please provide at least two document IDs to merge.", err=True)
        return

    documents_to_merge_objects = []
    for d_id in doc_ids:
        if d_id not in all_docs_map:
            raise click.UsageError(f"Invalid document ID: {d_id}. Not found in storage.")
        documents_to_merge_objects.append(all_docs_map[d_id])

    for doc_obj in documents_to_merge_objects:
        click.echo(
            f'{doc_obj.id.split("-")[0]} {click.style(doc_obj.title, fg="yellow") or "<untitled>"}'
        )

    if not yes:  # pragma: no cover
        click.confirm(f"Merge these {len(documents_to_merge_objects)} documents?", abort=True)

    from docstore.merging import get_title_candidates, get_union_of_tags

    title_candidates = get_title_candidates(documents_to_merge_objects)
    new_title: str
    if len(title_candidates) == 1:
        click.echo(f"Using common title: {click.style(title_candidates[0], fg='blue')}")
        new_title = title_candidates[0]
    else:
        click.echo(f'\nGuessed title: {click.style(title_candidates[0], fg="blue")}')
        if yes or click.confirm("Use title?"):
            new_title = title_candidates[0]
        else:  # pragma: no cover
            edited_title = click.edit("\n".join(title_candidates))
            new_title = edited_title.strip() if edited_title is not None else title_candidates[0]

    all_tags = get_union_of_tags(documents_to_merge_objects)
    new_tags: list[str]
    click.echo(f"\nGuessed tags: {click.style(', '.join(all_tags), fg='blue')}")
    if yes or click.confirm("Use tags?"):
        new_tags = all_tags
    else:  # pragma: no cover
        edited_tags_str = click.edit("\n".join(all_tags))
        new_tags = edited_tags_str.strip().splitlines() if edited_tags_str is not None else all_tags
    
    # The target document (first in list) will absorb others.
    target_doc_id = doc_ids[0]
    source_doc_ids = doc_ids[1:]

    merged_doc = None
    current_target_doc_id = target_doc_id
    for source_doc_id in source_doc_ids:
        click.echo(f"Merging {source_doc_id} into {current_target_doc_id}...")
        try:
            merged_doc = documents_api.pairwise_merge_documents(
                storage=storage_instance,
                doc1_id=current_target_doc_id, 
                doc2_id=source_doc_id,
                new_title=new_title, # Title and tags are set for the final merged doc
                new_tags=new_tags
            )
            # The ID of the merged document (doc1) remains the same.
            # No need to update current_target_doc_id unless the function returned a new entity
            # (which it doesn't, it modifies doc1 and removes doc2).
        except Exception as e:
            click.echo(f"Error merging {source_doc_id} into {current_target_doc_id}: {e}", err=True)
            # Decide if to continue or abort all. For now, try to continue.
            # If a merge fails, the target_doc_id might not have all files.
    
    if merged_doc:
        click.echo(f"Merge complete. Final document ID: {merged_doc.id}")
    else:
        click.echo("Merge operation did not complete successfully for all pairs.", err=True)


def find_similar_pairs(
    tags: Iterable[str], *, required_similarity: int = 80
) -> Iterable[tuple[str, str]]:
    """
    Find pairs of similar-looking tags in the collection ``tags``.

    Increase ``required_similarity`` for stricter matching (=> less results).
    """
    import itertools

    from rapidfuzz import fuzz

    for t1, t2 in itertools.combinations(sorted(tags), 2):
        # utilities:gas, utilities:electricity
        if os.path.commonprefix([t1, t2]).endswith(":"):
            continue

        # utilities, utilities:gas
        if t1.startswith(f"{t2}:") or t2.startswith(f"{t1}:"):
            continue

        if fuzz.ratio(t1, t2) > required_similarity:
            yield (t1, t2)


@main.command(help="Show tags that might be similar")
@click.pass_context # Changed to pass_context
def show_similar_tags(ctx: click.Context) -> None:
    import collections
    from docstore.documents import read_documents # Uses storage_instance

    cli_root_path = ctx.obj["cli_root"]
    storage_instance = _get_storage_instance(ctx, cli_root_path)
    
    documents = read_documents(storage_instance)
    tags: dict[str, int] = collections.Counter()

    for doc in documents:
        for t in doc.tags:
            tags[t] += 1

    for t1, t2 in find_similar_pairs(set(tags)):
        print("%3d %s" % (tags[t1], t1))
        print("%3d %s" % (tags[t2], t2))
        print("")
