import pathlib
import typing
import json # For read_metadata if S3 object doesn't exist
import os # For parsing s3 keys if needed

import boto3
import botocore.exceptions


class Storage(typing.Protocol):
    """
    Abstract interface for document and metadata storage.
    """

    def save_document_file(self, original_filename: str, contents: bytes, doc_id: str) -> str:
        """
        Saves a document file and returns its stored path.
        `doc_id` can be used to group files belonging to the same document.
        """
        ...

    def get_document_file(self, stored_path: str) -> bytes:
        """
        Retrieves a document file by its stored path.
        """
        ...

    def get_document_original_filename(self, stored_path: str) -> str:
        """
        Retrieves the original filename of a document given its stored path.
        """
        ...

    def save_thumbnail_file(self, original_doc_path: str, contents: bytes) -> str:
        """
        Saves a thumbnail file and returns its stored path.
        """
        ...

    def get_thumbnail_file(self, stored_thumbnail_path: str) -> bytes:
        """
        Retrieves a thumbnail file.
        """
        ...

    def list_document_files(self) -> list[str]:
        """
        Lists all stored document paths.
        """
        ...

    def save_metadata(self, metadata_json: str) -> None:
        """
        Saves the complete metadata string (contents of documents.json).
        """
        ...

    def read_metadata(self) -> str:
        """
        Reads the complete metadata string.
        """
        ...

    def normalize_filename(self, filename: str) -> str:
        """
        Normalizes a filename.
        """
        ...

    def delete_document_file(self, stored_path: str) -> None:
        """
        Deletes a document file by its stored path.
        """
        ...

    def delete_thumbnail_file(self, stored_thumbnail_path: str) -> None:
        """
        Deletes a thumbnail file by its stored path.
        """
        ...


class LocalStorage(Storage):
    """
    Stores documents and thumbnails on the local filesystem.
    """

    def __init__(self, root_path: pathlib.Path):
        self.root_path = root_path
        self.files_dir = root_path / "files"
        self.thumbnails_dir = root_path / "thumbnails"
        self.metadata_file = root_path / "documents.json"

        # Ensure directories exist
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.thumbnails_dir.mkdir(parents=True, exist_ok=True)

    def normalize_filename(self, filename: str) -> str:
        # Basic normalization, can be expanded based on existing logic
        return "".join(c if c.isalnum() or c in ('.', '-', '_') else '_' for c in filename)

    def save_document_file(self, original_filename: str, contents: bytes, doc_id: str) -> str:
        normalized_original_filename = self.normalize_filename(original_filename)
        # Use doc_id to create a subdirectory for the document's files
        document_specific_dir = self.files_dir / doc_id
        document_specific_dir.mkdir(parents=True, exist_ok=True)
        
        stored_filename = f"{normalized_original_filename}"
        stored_path = document_specific_dir / stored_filename
        
        # Ensure unique filename if conflicts occur, though less likely with doc_id subdirectories
        counter = 0
        while stored_path.exists():
            counter += 1
            base, ext = pathlib.Path(normalized_original_filename).stem, pathlib.Path(normalized_original_filename).suffix
            stored_filename = f"{base}_{counter}{ext}"
            stored_path = document_specific_dir / stored_filename
            
        stored_path.write_bytes(contents)
        # Return path relative to the root_path for consistency
        return str(stored_path.relative_to(self.root_path))

    def get_document_file(self, stored_path: str) -> bytes:
        abs_path = self.root_path / stored_path
        if not abs_path.exists():
            raise FileNotFoundError(f"Document file not found: {stored_path}")
        return abs_path.read_bytes()

    def get_document_original_filename(self, stored_path: str) -> str:
        # This is a placeholder. The current implementation in documents.py
        # reconstructs this from the stored filename if it was previously
        # saved with an ID prefix. Or it looks up in documents.json.
        # For now, we can return the filename part of the stored_path.
        # This will need to be revisited to integrate with metadata.
        # A more robust solution might involve storing original filename as metadata
        # or encoding it in a way that it can be retrieved.
        # For now, let's assume the stored_path's filename IS the original filename
        # or at least the one we can retrieve directly.
        # If the `save_document_file` stores it as `doc_id/original_filename.ext`,
        # then `pathlib.Path(stored_path).name` would be correct.
        return pathlib.Path(stored_path).name

    def save_thumbnail_file(self, original_doc_path: str, contents: bytes) -> str:
        # original_doc_path is relative to root_path, e.g., "files/doc1/document.pdf"
        # We want to base the thumbnail name on the original document's name.
        original_doc_name = pathlib.Path(original_doc_path).name
        thumbnail_filename = f"{original_doc_name}.jpg" # Assuming thumbnails are JPEGs
        
        # Store thumbnails in a structure mirroring the files if needed, or flat
        # For simplicity, let's try a flat structure first, using a hash or unique ID
        # of the original_doc_path to ensure uniqueness if names clash.
        # However, the task asks to return its stored path, implying we can reconstruct it.
        # Let's use a similar naming convention to original files but in thumbnails_dir.
        
        normalized_thumb_filename = self.normalize_filename(thumbnail_filename)
        
        # Option 1: Flat structure in thumbnails_dir
        # stored_thumbnail_path = self.thumbnails_dir / normalized_thumb_filename
        # This might lead to collisions if multiple docs have same name.
        
        # Option 2: Mirroring structure (e.g., thumbnails/doc_id/filename.jpg)
        # This requires knowing the doc_id or deriving it from original_doc_path.
        # If original_doc_path is "files/doc_id/filename.ext", then parts are:
        # Path(original_doc_path).parts -> ('files', 'doc_id', 'filename.ext')
        doc_id_folder = pathlib.Path(original_doc_path).parent.name # This should be doc_id
        thumbnail_folder_for_doc = self.thumbnails_dir / doc_id_folder
        thumbnail_folder_for_doc.mkdir(parents=True, exist_ok=True)
        
        stored_thumbnail_path = thumbnail_folder_for_doc / normalized_thumb_filename

        counter = 0
        while stored_thumbnail_path.exists():
            counter += 1
            base, ext = pathlib.Path(normalized_thumb_filename).stem, pathlib.Path(normalized_thumb_filename).suffix
            # Ensure the .jpg is preserved or re-added if stemming removed it
            if not ext: # if original name had no ext, stem might take all, and ext is .jpg
                 base = base.replace('.jpg','') # remove .jpg if it was part of base
                 ext = '.jpg'

            stored_thumbnail_path = thumbnail_folder_for_doc / f"{base}_{counter}{ext}"
            
        stored_thumbnail_path.write_bytes(contents)
        return str(stored_thumbnail_path.relative_to(self.root_path))

    def get_thumbnail_file(self, stored_thumbnail_path: str) -> bytes:
        abs_path = self.root_path / stored_thumbnail_path
        if not abs_path.exists():
            raise FileNotFoundError(f"Thumbnail file not found: {stored_thumbnail_path}")
        return abs_path.read_bytes()

    def list_document_files(self) -> list[str]:
        # Lists all files within the files_dir, recursively.
        # This lists physical files, not necessarily only those tracked by documents.json.
        all_files = []
        for p in self.files_dir.rglob("*"):
            if p.is_file():
                all_files.append(str(p.relative_to(self.root_path)))
        return all_files

    def save_metadata(self, metadata_json: str) -> None:
        self.metadata_file.write_text(metadata_json, encoding='utf-8')

    def read_metadata(self) -> str:
        if not self.metadata_file.exists():
            # Return empty JSON object string or raise error?
            # For now, let's match existing behavior which might expect a file.
            # If documents.json can be non-existent initially, return "{}"
            return "{}" 
        return self.metadata_file.read_text(encoding='utf-8')

    def delete_document_file(self, stored_path: str) -> None:
        abs_path = self.root_path / stored_path
        try:
            abs_path.unlink()
            # Optionally, try to remove the doc_id parent directory if it's empty
            try:
                abs_path.parent.rmdir() # Only removes if empty
            except OSError: # Directory not empty or other issue
                pass 
        except FileNotFoundError:
            # Log or handle as appropriate if the file is expected to exist
            print(f"Warning: Document file not found for deletion: {stored_path}")
        except Exception as e:
            print(f"Error deleting document file {stored_path}: {e}")
            raise

    def delete_thumbnail_file(self, stored_thumbnail_path: str) -> None:
        abs_path = self.root_path / stored_thumbnail_path
        try:
            abs_path.unlink()
            # Optionally, try to remove the doc_id parent directory if it's empty
            try:
                abs_path.parent.rmdir() # Only removes if empty
            except OSError: # Directory not empty or other issue
                pass
        except FileNotFoundError:
            print(f"Warning: Thumbnail file not found for deletion: {stored_thumbnail_path}")
        except Exception as e:
            print(f"Error deleting thumbnail file {stored_thumbnail_path}: {e}")
            raise

# Example usage (for testing purposes, will be removed or moved)
if __name__ == '__main__':
    # This part is just for initial testing and wouldn't be in the final module.
    # It requires a directory structure to be set up.
    # To run this:
    # 1. mkdir -p /tmp/docstore_root/files /tmp/docstore_root/thumbnails
    # 2. python src/docstore/storage.py

    print("Running LocalStorage example...")
    temp_root = pathlib.Path("/tmp/docstore_root")
    temp_root.mkdir(exist_ok=True)
    
    storage = LocalStorage(root_path=temp_root)

    # Test normalize_filename
    print(f"Normalized 'test file.txt': {storage.normalize_filename('test file.txt')}")

    # Test save_document_file
    doc_id_1 = "doc123"
    doc_path_1 = storage.save_document_file("My Document.pdf", b"PDF content here", doc_id_1)
    print(f"Saved document: {doc_path_1}")
    assert doc_path_1 == f"files/{doc_id_1}/My_Document.pdf"
    
    doc_path_1_again = storage.save_document_file("My Document.pdf", b"New PDF content", doc_id_1)
    print(f"Saved document again (should be unique): {doc_path_1_again}")
    assert doc_path_1_again == f"files/{doc_id_1}/My_Document_1.pdf"


    # Test get_document_file
    content = storage.get_document_file(doc_path_1_again)
    print(f"Retrieved document content: {content.decode()}")
    assert content == b"New PDF content"

    # Test get_document_original_filename (basic impl)
    original_name = storage.get_document_original_filename(doc_path_1)
    print(f"Retrieved original filename (basic): {original_name}")
    assert original_name == "My_Document.pdf" # Based on current simple impl

    # Test save_thumbnail_file
    thumb_path_1 = storage.save_thumbnail_file(doc_path_1, b"Thumbnail JPEG content")
    print(f"Saved thumbnail: {thumb_path_1}")
    assert thumb_path_1 == f"thumbnails/{doc_id_1}/My_Document.pdf.jpg"
    
    thumb_path_1_again = storage.save_thumbnail_file(doc_path_1, b"New Thumbnail JPEG content")
    print(f"Saved thumbnail again: {thumb_path_1_again}")
    assert thumb_path_1_again == f"thumbnails/{doc_id_1}/My_Document.pdf_1.jpg"


    # Test get_thumbnail_file
    thumb_content = storage.get_thumbnail_file(thumb_path_1_again)
    print(f"Retrieved thumbnail content: {thumb_content.decode()}")
    assert thumb_content == b"New Thumbnail JPEG content"

    # Test list_document_files
    doc_id_2 = "doc456"
    storage.save_document_file("Another Doc.txt", b"text content", doc_id_2)
    all_docs = storage.list_document_files()
    print(f"All documents: {all_docs}")
    assert sorted(all_docs) == sorted([
        f"files/{doc_id_1}/My_Document.pdf",
        f"files/{doc_id_1}/My_Document_1.pdf",
        f"files/{doc_id_2}/Another_Doc.txt"
    ])

    # Test metadata
    metadata_content = '{"key": "value", "count": 10}'
    storage.save_metadata(metadata_content)
    retrieved_metadata = storage.read_metadata()
    print(f"Retrieved metadata: {retrieved_metadata}")
    assert retrieved_metadata == metadata_content
    
    # Test reading non-existent metadata
    (temp_root / "documents.json").unlink()
    assert storage.read_metadata() == "{}"

    # Test delete operations
    storage.save_document_file("delete_me.txt", b"to be deleted", "del_doc_1")
    storage.save_thumbnail_file("files/del_doc_1/delete_me.txt", b"thumb to delete")
    
    doc_to_delete_path = "files/del_doc_1/delete_me.txt"
    thumb_to_delete_path = "thumbnails/del_doc_1/delete_me.txt.jpg"

    assert (temp_root / doc_to_delete_path).exists()
    assert (temp_root / thumb_to_delete_path).exists()

    storage.delete_document_file(doc_to_delete_path)
    storage.delete_thumbnail_file(thumb_to_delete_path)

    assert not (temp_root / doc_to_delete_path).exists()
    assert not (temp_root / thumb_to_delete_path).exists()
    # Check if parent dir was removed (best effort)
    # print(list((temp_root / "files/del_doc_1").iterdir())) -> should be empty or dir gone
    # print(list((temp_root / "thumbnails/del_doc_1").iterdir()))

    # Test deleting non-existent files (should not raise error, but print warning)
    storage.delete_document_file("files/non_existent_doc.txt")
    storage.delete_thumbnail_file("thumbnails/non_existent_thumb.jpg")

    print("LocalStorage example completed successfully.")

    # Clean up temp directory
    import shutil
    shutil.rmtree(temp_root)
    print(f"Cleaned up {temp_root}")
