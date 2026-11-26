"""Onward Logistics delivery photos: one deployable that serves the UI and the photo logic.

- Photos go to a private S3 bucket. Their details (title, description, merchant, order,
  rider, city) go to PostgreSQL with the photo's S3 key; the photo itself never does.
- The title and description are also saved on the S3 object, so the copy in the second
  Region is still evidence on its own if the database is lost with the primary Region.
- Photos are shown through CloudFront only, with signed URLs that expire after a few
  minutes. The bucket refuses direct requests.
- Delete is recoverable: the record is marked deleted and S3 adds a delete marker. Restore
  copies the last version back and clears the mark. The app cannot remove versions.

Configuration comes from the environment (set by the ECS task definition); the database
password and the signing key are injected by ECS from Secrets Manager.
"""
import json
import os
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import boto3
import psycopg
from botocore.signers import CloudFrontSigner
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from flask import Flask, abort, redirect, render_template, request, url_for
from psycopg.rows import dict_row

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB per upload

OWNER_NAME = os.environ.get("OWNER_NAME", "Gabriel Anyaele")
LAB_NAME = os.environ.get("LAB_NAME", "Storing and Serving Delivery Photos for Onward Logistics")
APP_VERSION = os.environ.get("APP_VERSION", "dev")
BUCKET = os.environ.get("PHOTOS_BUCKET", "")
CDN_DOMAIN = os.environ.get("CDN_DOMAIN", "")
SIGNING_KEY_ID = os.environ.get("SIGNING_KEY_ID", "")
SIGNED_URL_MINUTES = int(os.environ.get("SIGNED_URL_MINUTES", "5"))
ALLOWED_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}

s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "eu-west-1"))


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def db():
    return psycopg.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        dbname=os.environ.get("DB_NAME", "photos"),
        user=os.environ.get("DB_USER", "onward"),
        password=os.environ.get("DB_PASSWORD", ""),
        sslmode=os.environ.get("DB_SSLMODE", "require"),
        connect_timeout=5,
        row_factory=dict_row,
    )


SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
    id           SERIAL PRIMARY KEY,
    s3_key       TEXT NOT NULL UNIQUE,      -- links the record to its photo in S3
    title        TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    merchant     TEXT NOT NULL DEFAULT '',
    order_ref    TEXT NOT NULL DEFAULT '',
    rider        TEXT NOT NULL DEFAULT '',
    city         TEXT NOT NULL DEFAULT '',
    content_type TEXT NOT NULL,
    size_bytes   INTEGER NOT NULL,
    uploaded_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at   TIMESTAMPTZ                -- set by delete, cleared by restore
);
"""


def ensure_schema():
    with db() as conn:
        conn.execute(SCHEMA)


_schema_ready = False


@app.before_request
def _schema():
    global _schema_ready
    if not _schema_ready and request.endpoint not in ("health", "static"):
        ensure_schema()
        _schema_ready = True


# ---------------------------------------------------------------------------
# CloudFront signed URLs
# ---------------------------------------------------------------------------
_private_key = None


def _rsa_signer(message):
    global _private_key
    if _private_key is None:
        pem = os.environ["SIGNING_PRIVATE_KEY"].encode()
        _private_key = serialization.load_pem_private_key(pem, password=None)
    return _private_key.sign(message, padding.PKCS1v15(), hashes.SHA1())  # CloudFront requires SHA-1


def photo_url(key):
    """A link to the photo through CloudFront that stops working after SIGNED_URL_MINUTES."""
    if not (CDN_DOMAIN and SIGNING_KEY_ID):
        return ""
    expires = datetime.now(timezone.utc) + timedelta(minutes=SIGNED_URL_MINUTES)
    return CloudFrontSigner(SIGNING_KEY_ID, _rsa_signer).generate_presigned_url(
        f"https://{CDN_DOMAIN}/{quote(key)}", date_less_than=expires)


# ---------------------------------------------------------------------------
# Which server answered (shows a container or zone being lost)
# ---------------------------------------------------------------------------
def _server():
    meta = os.environ.get("ECS_CONTAINER_METADATA_URI_V4")
    if meta:
        try:
            task = json.load(urllib.request.urlopen(f"{meta}/task", timeout=1))
            return {"task": task["TaskARN"].rsplit("/", 1)[-1][:12],
                    "zone": task.get("AvailabilityZone", "unknown")}
        except (OSError, KeyError, ValueError):
            pass
    return {"task": "local", "zone": "local"}


SERVER = _server() | {"version": APP_VERSION}


@app.context_processor
def _common():
    return {"owner": OWNER_NAME, "lab": LAB_NAME, "server": SERVER, "minutes": SIGNED_URL_MINUTES}


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@app.get("/")
def gallery():
    with db() as conn:
        photos = conn.execute(
            "SELECT * FROM photos WHERE deleted_at IS NULL ORDER BY uploaded_at DESC LIMIT 60").fetchall()
    for p in photos:
        p["url"] = photo_url(p["s3_key"])
    return render_template("gallery.html", photos=photos, notice=request.args.get("notice"))


@app.get("/upload")
def upload_form():
    return render_template("upload.html", error=request.args.get("error"))


def _field(name, limit):
    return request.form.get(name, "").strip()[:limit]


@app.post("/upload")
def upload():
    photo = request.files.get("photo")
    title = _field("title", 120)
    if not photo or not title:
        return redirect(url_for("upload_form", error="Choose a photo and give it a title."))
    content_type = photo.mimetype
    if content_type not in ALLOWED_TYPES:
        return redirect(url_for("upload_form", error="Photos must be JPEG, PNG or WebP."))
    details = {
        "title": title,
        "description": _field("description", 500),
        "merchant": _field("merchant", 80),
        "order_ref": _field("order_ref", 40),
        "rider": _field("rider", 80),
        "city": _field("city", 80),
    }
    data = photo.read()
    key = f"photos/{datetime.now(timezone.utc):%Y/%m/%d}/{uuid.uuid4()}.{ALLOWED_TYPES[content_type]}"

    # Details also ride on the object (URL-encoded: S3 metadata must be ASCII), so the
    # replica in the second Region is complete evidence without the database.
    s3.put_object(Bucket=BUCKET, Key=key, Body=data, ContentType=content_type,
                  Metadata={k: quote(v) for k, v in details.items()})
    with db() as conn:
        conn.execute(
            """INSERT INTO photos (s3_key, title, description, merchant, order_ref, rider, city,
                                   content_type, size_bytes)
               VALUES (%(key)s, %(title)s, %(description)s, %(merchant)s, %(order_ref)s, %(rider)s,
                       %(city)s, %(content_type)s, %(size)s)""",
            details | {"key": key, "content_type": content_type, "size": len(data)})
    return redirect(url_for("gallery", notice=f"Uploaded “{title}”."))


@app.post("/photos/<int:photo_id>/delete")
def delete(photo_id):
    with db() as conn:
        row = conn.execute("UPDATE photos SET deleted_at = now() WHERE id = %s AND deleted_at IS NULL "
                           "RETURNING s3_key, title", (photo_id,)).fetchone()
    if not row:
        abort(404)
    s3.delete_object(Bucket=BUCKET, Key=row["s3_key"])  # versioned bucket: adds a delete marker only
    return redirect(url_for("gallery", notice=f"Deleted “{row['title']}”. It can be restored."))


@app.get("/deleted")
def deleted():
    with db() as conn:
        photos = conn.execute(
            "SELECT * FROM photos WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC LIMIT 60").fetchall()
    return render_template("deleted.html", photos=photos)


@app.post("/photos/<int:photo_id>/restore")
def restore(photo_id):
    with db() as conn:
        row = conn.execute("SELECT s3_key, title FROM photos WHERE id = %s AND deleted_at IS NOT NULL",
                           (photo_id,)).fetchone()
        if not row:
            abort(404)
        key = row["s3_key"]
        versions = s3.list_object_versions(Bucket=BUCKET, Prefix=key).get("Versions", [])
        versions = [v for v in versions if v["Key"] == key]
        if not versions:
            return redirect(url_for("deleted"))
        latest = max(versions, key=lambda v: v["LastModified"])
        s3.copy_object(Bucket=BUCKET, Key=key, MetadataDirective="COPY",
                       CopySource={"Bucket": BUCKET, "Key": key, "VersionId": latest["VersionId"]})
        conn.execute("UPDATE photos SET deleted_at = NULL WHERE id = %s", (photo_id,))
    return redirect(url_for("gallery", notice=f"Restored “{row['title']}”."))


@app.get("/about")
def about():
    return render_template("about.html")


@app.get("/health")
def health():
    # Deliberately does not touch the database: during a Multi-AZ failover the tasks
    # should stay in service and recover, not be replaced.
    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
