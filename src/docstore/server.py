import collections
import datetime
import functools
import hashlib
# import os # os.path.abspath might not be needed anymore
# import pathlib # pathlib.Path might not be needed directly here
import secrets
import typing
import urllib.parse
from urllib.parse import parse_qsl, urlparse, urlencode

from flask import (
    Flask,
    Response as FlaskResponse,
    # make_response, # Will construct FlaskResponse directly
    render_template,
    request,
    # send_file, # Replaced by storage interaction
    # send_from_directory, # Replaced by storage interaction
    current_app, # To access storage from app config
    abort, # For 404 errors
)
import hyperlink
import smartypants
from werkzeug.middleware.profiler import ProfilerMiddleware

# find_original_filename is removed, read_documents is updated
from .documents import read_documents
from .models import Document
from .storage import Storage # Import Storage protocol
from .tag_cloud import TagCloud
from .tag_list import render_tag_list
from .text_utils import hostname, pretty_date


def tags_with_prefix(document: Document, prefix: str) -> list[str]:
    return [t for t in document.tags if t.startswith(prefix)]


def tags_without_prefix(document: Document, prefix: str) -> list[str]:
    return [t for t in document.tags if not t.startswith(prefix)]


def url_without_sortby(u: str) -> str:
    url = hyperlink.URL.from_text(u)
    return str(url.remove("sortBy"))


# The old serve_file function is removed. New function serve_document will handle this.

def create_app(title: str, storage: Storage, thumbnail_width: int) -> Flask:
    app = Flask(__name__)

    app.config["storage"] = storage
    app.config["THUMBNAIL_WIDTH"] = thumbnail_width # Still used by templates

    app.jinja_env.trim_blocks = True
    app.jinja_env.lstrip_blocks = True

    app.jinja_env.filters["hostname"] = hostname
    app.jinja_env.filters["pretty_date"] = lambda d: pretty_date(
        d, now=datetime.datetime.now()
    )
    app.jinja_env.filters["render_tag_list"] = render_tag_list
    app.jinja_env.filters["smartypants"] = smartypants.smartypants
    app.jinja_env.filters["url_without_sortby"] = url_without_sortby

    app.jinja_env.filters["tags_with_prefix"] = tags_with_prefix
    app.jinja_env.filters["tags_without_prefix"] = tags_without_prefix

    @app.route("/")
    def list_documents() -> str:
        active_storage: Storage = current_app.config["storage"]
        request_tags = set(request.args.getlist("tag"))
        documents = [
            doc for doc in read_documents(active_storage) if request_tags.issubset(set(doc.tags))
        ]

        tag_tally: dict[str, int] = collections.Counter()
        for doc in documents:
            for t in doc.tags:
                tag_tally[t] += 1

        try:
            page = int(request.args["page"])
        except KeyError:
            page = 1

        sort_by = request.args.get("sortBy", "date (newest first)")

        if sort_by.startswith("date"):
            sort_key = lambda d: d.date_saved  # noqa
        elif sort_by.startswith("title"):
            sort_key = lambda d: d.title.lower()  # noqa
        elif sort_by == "random":
            if page == 1:
                app.config["_RANDOM_SEED"] = secrets.token_bytes()
            # Ensure _RANDOM_SEED is initialized if page > 1 but seed is not set (e.g. direct URL access)
            if "_RANDOM_SEED" not in app.config:
                 app.config["_RANDOM_SEED"] = secrets.token_bytes()
            seed = app.config["_RANDOM_SEED"]

            def sort_key(d: Document) -> str:
                h = hashlib.md5()
                h.update(d.id.encode("utf8"))
                h.update(seed)
                return h.hexdigest()
        else:
            # Ensure to re-raise or handle appropriately for security
            # For now, let it fall through to Flask's default error handling for unexpected values
            pass # Or raise ValueError(f"Unrecognised sortBy query parameter: {sort_by}")

        if sort_by in {"date (newest first)", "title (Z to A)"}:
            sort_reverse = True
        else:
            sort_reverse = False
        
        # Handle case where sort_key might not be defined if sort_by is invalid
        # and not caught by an explicit raise earlier.
        # Defaulting to date sort or another safe default.
        if 'sort_key' not in locals():
            sort_key = lambda d: d.date_saved # Default sort
            sort_reverse = True # Default sort order

        html = render_template(
            "index.html",
            documents=sorted(documents, key=sort_key, reverse=sort_reverse),
            request_tags=request_tags,
            query_string=tuple(parse_qsl(urlparse(request.url).query)),
            tag_tally=tag_tally,
            title=title,
            page=page,
            sort_by=sort_by,
            TagCloud=TagCloud,
        )

        return html

    @app.route("/thumbnail/<path:key>")
    def serve_thumbnail(key: str) -> FlaskResponse:
        active_storage: Storage = current_app.config["storage"]
        try:
            thumbnail_bytes = active_storage.get_thumbnail_file(key)
            # Assuming JPEG, S3Storage saves as .jpg. LocalStorage also does.
            return FlaskResponse(thumbnail_bytes, mimetype="image/jpeg")
        except FileNotFoundError:
            abort(404, description="Thumbnail not found")
        except Exception as e: # Catch other storage related errors
            print(f"Error serving thumbnail {key}: {e}")
            abort(500, description="Error serving thumbnail")


    @app.route("/file/<path:key>")
    def serve_document(key: str) -> FlaskResponse:
        active_storage: Storage = current_app.config["storage"]
        try:
            file_contents = active_storage.get_document_file(key)
            original_filename = active_storage.get_document_original_filename(key)

            encoded_filename = urllib.parse.quote(original_filename, encoding="utf-8")
            
            # Create a response with the file content
            response = FlaskResponse(file_contents)
            
            # Set Content-Disposition header for download with original filename
            response.headers["Content-Disposition"] = f"attachment; filename*=utf-8''{encoded_filename}"
            
            # Set a generic Content-Type; this could be improved with a mimetype library
            # For S3, ContentType might be stored with the object.
            # For LocalStorage, it's not stored by default.
            response.mimetype = "application/octet-stream" # Default fallback
            
            return response
        except FileNotFoundError:
            abort(404, description="File not found")
        except Exception as e: # Catch other storage related errors
            print(f"Error serving file {key}: {e}")
            abort(500, description="Error serving file")


    QueryString: typing.TypeAlias = list[tuple[str, str]]

    @app.template_filter("add_tag")
    @functools.lru_cache()
    def add_tag(query_string: QueryString, tag: str) -> str:
        return "?" + urlencode(
            [(k, v) for k, v in query_string if k != "page"] + [("tag", tag)]
        )

    @app.template_filter("remove_tag")
    def remove_tag(query_string: QueryString, tag: str) -> str:
        return "?" + urlencode(
            [(k, v) for k, v in query_string if (k, v) != ("tag", tag)]
        )

    @app.template_filter("set_page")
    @functools.lru_cache()
    def set_page(query_string: QueryString, page: int) -> str:
        pageless_qs = [(k, v) for k, v in query_string if k != "page"]
        if page == 1:
            return "?" + urlencode(pageless_qs)
        else:
            return "?" + urlencode(pageless_qs + [("page", page)])

    return app


def run_profiler(app: Flask, *, host: str, port: int) -> None:  # pragma: no cover
    app.config["PROFILE"] = True
    app.wsgi_app = ProfilerMiddleware(app.wsgi_app, restrictions=[30])  # type: ignore
    app.run(host=host, port=port, debug=True)


def run_server(
    app: Flask, *, host: str, port: int, debug: bool
) -> None:  # pragma: no cover
    app.run(host=host, port=port, debug=debug)
