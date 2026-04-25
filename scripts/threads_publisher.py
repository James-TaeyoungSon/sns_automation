import os
import time

import requests


def post_text_to_threads(text: str, link_url: str | None = None) -> str:
    """Post a text-only Threads post and return the published post ID."""
    access_token = os.environ.get("THREADS_ACCESS_TOKEN")
    if not access_token:
        raise RuntimeError("THREADS_ACCESS_TOKEN is not set.")
    if "|" in access_token:
        raise RuntimeError(
            "THREADS_ACCESS_TOKEN looks like an app/client token. "
            "Use a Threads Graph API user access token with publishing permission."
        )

    configured_user_id = os.environ.get("THREADS_USER_ID", "")
    user_id = configured_user_id if configured_user_id.isdigit() else "me"

    create_url = f"https://graph.threads.net/v1.0/{user_id}/threads"
    payload = {
        "media_type": "TEXT",
        "text": text,
        "access_token": access_token,
    }
    if link_url:
        payload["link_attachment"] = link_url

    response = requests.post(create_url, data=payload, timeout=30)
    if response.status_code != 200:
        raise RuntimeError(f"Error creating Threads text container: {_safe_response(response)}")

    creation_id = response.json().get("id")
    if not creation_id:
        raise RuntimeError("Threads did not return a creation container ID.")

    time.sleep(3)

    publish_url = f"https://graph.threads.net/v1.0/{user_id}/threads_publish"
    publish_response = requests.post(
        publish_url,
        data={"creation_id": creation_id, "access_token": access_token},
        timeout=30,
    )
    if publish_response.status_code != 200:
        raise RuntimeError(
            f"Error publishing Threads text post: {_safe_response(publish_response)}"
        )

    post_id = publish_response.json().get("id")
    if not post_id:
        raise RuntimeError("Threads publish response did not include a post ID.")
    return post_id


def _safe_response(response: requests.Response):
    try:
        return response.json()
    except ValueError:
        return response.text[:1000]
