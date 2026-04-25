import base64
import os

import requests
from nacl import encoding, public


GH_PAT = os.environ["GH_PAT"]
GH_REPO = os.environ.get("GITHUB_REPOSITORY", "James-TaeyoungSon/sns_automation")


def refresh_instagram(token: str) -> str:
    response = requests.get(
        "https://graph.instagram.com/refresh_access_token",
        params={"grant_type": "ig_refresh_token", "access_token": token},
        timeout=20,
    )
    data = response.json()
    if "access_token" not in data:
        raise RuntimeError(f"Instagram token refresh failed: {data}")
    print_token_result("Instagram", data)
    return data["access_token"]


def refresh_threads(token: str) -> str:
    response = requests.get(
        "https://graph.threads.net/refresh_access_token",
        params={"grant_type": "th_refresh_token", "access_token": token},
        timeout=20,
    )
    data = response.json()
    if "access_token" not in data:
        raise RuntimeError(f"Threads token refresh failed: {data}")
    print_token_result("Threads", data)
    return data["access_token"]


def print_token_result(name: str, data: dict) -> None:
    expires_in = int(data.get("expires_in", 0))
    if expires_in:
        print(f"[OK] {name} token refreshed. Expires in {expires_in // 86400} days.")
    else:
        print(f"[OK] {name} token refreshed.")


def update_secret(name: str, value: str) -> None:
    headers = {
        "Authorization": f"Bearer {GH_PAT}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    key_response = requests.get(
        f"https://api.github.com/repos/{GH_REPO}/actions/secrets/public-key",
        headers=headers,
        timeout=10,
    )
    key_response.raise_for_status()
    key_data = key_response.json()

    public_key = public.PublicKey(key_data["key"].encode(), encoding.Base64Encoder())
    encrypted = base64.b64encode(
        public.SealedBox(public_key).encrypt(value.encode())
    ).decode()

    secret_response = requests.put(
        f"https://api.github.com/repos/{GH_REPO}/actions/secrets/{name}",
        headers=headers,
        json={"encrypted_value": encrypted, "key_id": key_data["key_id"]},
        timeout=10,
    )
    if secret_response.status_code not in (201, 204):
        raise RuntimeError(
            f"GitHub secret update failed for {name}: "
            f"{secret_response.status_code} {secret_response.text}"
        )
    print(f"[OK] GitHub secret updated: {name}")


def main() -> None:
    print("=== Token refresh started ===")
    refreshed_any = False

    instagram_token = os.environ.get("IG_ACCESS_TOKEN")
    if instagram_token:
        update_secret("IG_ACCESS_TOKEN", refresh_instagram(instagram_token))
        refreshed_any = True
    else:
        print("[SKIP] IG_ACCESS_TOKEN is not set.")

    threads_token = os.environ.get("THREADS_ACCESS_TOKEN")
    if threads_token:
        update_secret("THREADS_ACCESS_TOKEN", refresh_threads(threads_token))
        refreshed_any = True
    else:
        print("[SKIP] THREADS_ACCESS_TOKEN is not set.")

    if not refreshed_any:
        raise RuntimeError("No refreshable tokens were provided.")

    print("=== Token refresh completed ===")


if __name__ == "__main__":
    main()
