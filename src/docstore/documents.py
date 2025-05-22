import datetime
import hashlib
import json
import os
import pathlib # Still needed for sha256 and other functions not yet refactored
import shutil # Still needed for functions not yet refactored
import typing

import cattr

from docstore.file_normalisation import normalised_filename_copy # For store_new_document
from docstore.models import (
    DocstoreEncoder,
    Document,
    File,
    Thumbnail,
    from_json,
    to_json,
)
from docstore.text_utils import slugify # For store_new_document
from docstore.thumbnails import create_thumbnail, get_dimensions # For store_new_document
from docstore.tint_colors import choose_tint_color # For store_new_document
from .storage import Storage # Import the Storage protocol


# Caching logic has been removed for simplification with Storage abstraction.
# It can be re-added later if performance requires it, potentially with
# a different mechanism that is compatible with various storage backends.

def read_documents(storage: Storage) -> list[Document]:
    """
    Get a list of all the documents from the storage backend.
    """
    try:
        metadata_json = storage.read_metadata()
        if not metadata_json or metadata_json == "{}": # Handle empty string or empty object for no documents
            return []
        return from_json(metadata_json)
    except FileNotFoundError: # Should be caught by storage.read_metadata returning "{}"
        return []
    except json.JSONDecodeError as e:
        # Log or handle corrupted metadata
        print(f"Error decoding metadata JSON: {e}")
        # Depending on desired robustness, could raise error or return empty list
        return []


def write_documents(*, storage: Storage, documents: list[Document]) -> None:
    """
    Writes the list of documents to the storage backend.
    """
    json_string = to_json(documents)
    storage.save_metadata(json_string)


def sha256_bytes(data: bytes) -> str:
    """Computes the SHA256 hash of a byte string."""
    h = hashlib.sha256()
    h.update(data)
    return f"sha256:{h.hexdigest()}"


def sha256(path: pathlib.Path) -> str: # Operates on local paths
    h = hashlib.sha256()
    with open(path, "rb") as infile:
        for byte_block in iter(lambda: infile.read(4096), b""):
            h.update(byte_block)

    return "sha256:%s" % h.hexdigest()


def store_new_document(
    *,
    storage: Storage,
    doc_id: str, # Expect doc_id to be pre-generated for the new Document object
    original_filename: str,
    file_content_bytes: bytes,
    title: str,
    tags: list[str],
    source_url: str | None,
    date_saved: datetime.datetime,
) -> Document:
    # 1. Save the main document file
    stored_doc_path = storage.save_document_file(
        original_filename=original_filename,
        contents=file_content_bytes,
        doc_id=doc_id  # Use the pre-generated doc_id for grouping
    )

    # 2. Create and save the thumbnail
    # This requires writing the original file to a temporary local path
    # because `create_thumbnail` from `thumbnails.py` expects a path.
    local_temp_thumbnail_path = None
    tmp_orig_file_path = None
    stored_thumbnail_path = "" # Default to empty if thumbnailing fails
    thumbnail_dimensions = None

    try:
        # Suffix helps `create_thumbnail` identify file type if it relies on extension
        with tempfile.NamedTemporaryFile(delete=False, suffix=f"_{original_filename}") as tmp_orig_file:
            tmp_orig_file.write(file_content_bytes)
            tmp_orig_file_path = tmp_orig_file.name
        
        local_temp_thumbnail_path = create_thumbnail(tmp_orig_file_path)
        
        if local_temp_thumbnail_path and os.path.exists(local_temp_thumbnail_path):
            thumbnail_bytes = pathlib.Path(local_temp_thumbnail_path).read_bytes()
            stored_thumbnail_path = storage.save_thumbnail_file(
                original_doc_path=stored_doc_path, # Pass the stored path of the *original* document
                contents=thumbnail_bytes
            )
            thumbnail_dimensions = get_dimensions(local_temp_thumbnail_path)
            tint_color_for_file = choose_tint_color(thumbnail_path=local_temp_thumbnail_path, file_path=tmp_orig_file_path)
            hex_tint_color = "#%02x%02x%02x" % tuple(
                int(component * 255) for component in tint_color_for_file
            )
        else:
            # Handle case where thumbnail creation failed silently or returned non-existent path
            # For now, we'll proceed without a thumbnail. Logging would be good here.
            print(f"Warning: Thumbnail creation failed for {original_filename}", file=sys.stderr)
            hex_tint_color = "#000000" # Default tint color
            
    except Exception as e:
        # Log thumbnailing errors but don't let them block document saving
        print(f"Error during thumbnail creation for {original_filename}: {e}", file=sys.stderr)
        hex_tint_color = "#000000" # Default tint color
    finally:
        if tmp_orig_file_path and os.path.exists(tmp_orig_file_path):
            os.unlink(tmp_orig_file_path)
        if local_temp_thumbnail_path and os.path.exists(local_temp_thumbnail_path):
            # Clean up the temp thumbnail file and its containing directory
            try:
                temp_thumb_dir = os.path.dirname(local_temp_thumbnail_path)
                os.unlink(local_temp_thumbnail_path)
                if os.path.exists(temp_thumb_dir) and not os.listdir(temp_thumb_dir): # Check if dir is empty
                    os.rmdir(temp_thumb_dir)
            except Exception as e: # pylint: disable=broad-except
                print(f"Warning: Failed to clean up temporary thumbnail files: {e}", file=sys.stderr)


    # 3. Create the Document model instance
    # The Document.id is generated automatically by the model if not provided.
    # We are using the provided doc_id for consistency with storage paths.
    new_document = Document(
        id=doc_id, # Ensure the Document model uses this ID
        title=title,
        date_saved=date_saved,
        tags=tags,
        files=[
            File(
                filename=original_filename,
                path=stored_doc_path,
                size=len(file_content_bytes),
                checksum=sha256_bytes(file_content_bytes),
                source_url=source_url,
                thumbnail=Thumbnail(
                    path=stored_thumbnail_path,
                    dimensions=thumbnail_dimensions,
                    tint_color=hex_tint_color, # Generated above
                ),
                date_saved=date_saved, # Should this be file specific? Copied from old logic.
            )
        ],
    )

    # 4. Update and save the metadata
    documents = read_documents(storage)
    documents.append(new_document)
    write_documents(storage=storage, documents=documents)

    return new_document


def pairwise_merge_documents(
    storage: Storage,
    *,
    doc1_id: str, # Changed to accept IDs
    doc2_id: str, # Changed to accept IDs
    new_title: str,
    new_tags: list[str],
) -> Document:
    """
    Merge the files on two documents together.
    doc2's files are added to doc1. doc2 is then removed from metadata.
    No actual files are deleted from storage in this operation.
    """
    documents = read_documents(storage)
    
    doc1_list = [d for d in documents if d.id == doc1_id]
    if not doc1_list:
        raise ValueError(f"Document with ID {doc1_id} not found.")
    doc1 = doc1_list[0]

    doc2_list = [d for d in documents if d.id == doc2_id]
    if not doc2_list:
        raise ValueError(f"Document with ID {doc2_id} not found.")
    doc2 = doc2_list[0]

    # Prevent merging a document with itself
    if doc1.id == doc2.id:
        raise ValueError("Cannot merge a document with itself.")

    # Modify doc1: update metadata and extend files
    doc1.date_saved = min(doc1.date_saved, doc2.date_saved) # Or use current date?
    doc1.tags = sorted(list(set(doc1.tags + new_tags + doc2.tags))) # Merge tags, ensure uniqueness
    doc1.title = new_title
    doc1.files.extend(doc2.files) # Append files from doc2 to doc1

    # Remove doc2 from the list of documents
    documents_after_merge = [d for d in documents if d.id != doc2_id]
    
    # Update doc1 in the list (since we modified a copy)
    for i, d in enumerate(documents_after_merge):
        if d.id == doc1_id:
            documents_after_merge[i] = doc1
            break
            
    write_documents(storage=storage, documents=documents_after_merge)

    return doc1


def delete_document(storage: Storage, *, doc_id: str) -> None:
    """
    Deletes a document's metadata and its associated files from storage.
    """
    documents = read_documents(storage)
    
    doc_to_delete = None
    for doc in documents:
        if doc.id == doc_id:
            doc_to_delete = doc
            break
            
    if not doc_to_delete:
        print(f"Warning: Document with ID {doc_id} not found for deletion.")
        return

    # Delete associated files from storage
    for file_info in doc_to_delete.files:
        try:
            if file_info.path:
                storage.delete_document_file(file_info.path)
        except Exception as e:
            print(f"Error deleting document file {file_info.path} for doc {doc_id}: {e}", file=sys.stderr)
            
        try:
            if file_info.thumbnail and file_info.thumbnail.path:
                storage.delete_thumbnail_file(file_info.thumbnail.path)
        except Exception as e:
            print(f"Error deleting thumbnail file {file_info.thumbnail.path} for doc {doc_id}: {e}", file=sys.stderr)

    # Remove the document from metadata
    updated_documents = [d for d in documents if d.id != doc_id]
    write_documents(storage=storage, documents=updated_documents)


def find_original_filename(root: pathlib.Path, *, path: str) -> str:
    """
    Returns the name of the original file stored in this path.
    """
    documents = read_documents(root)
    for d in documents:
        for f in d.files:
            if f.path == os.path.relpath(path, root):
                return f.filename

    raise ValueError(f"Couldn't find file stored with path {path}")

# find_original_filename has been removed.
# Callers should use storage.get_document_original_filename(stored_path) instead.
# The `stored_path` is the path/key used by the Storage implementation (e.g., S3 key).
