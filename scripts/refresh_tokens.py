"""
Workflow 3: Instagram / Threads 액세스 토큰 자동 갱신
실행: 매월 1일 (GitHub Actions cron) — 60일 만료 전 갱신
"""
import os, base64, requests
from nacl import encoding, public

GH_PAT  = os.environ['GH_PAT']
GH_REPO = os.environ.get('GITHUB_REPOSITORY', 'James-TaeyoungSon/sns_automation')
IG_TOKEN = os.environ['IG_ACCESS_TOKEN']
TH_TOKEN = os.environ['THREADS_ACCESS_TOKEN']


def refresh_instagram() -> str:
    r = requests.get(
        'https://graph.instagram.com/refresh_access_token',
        params={'grant_type': 'ig_refresh_token', 'access_token': IG_TOKEN},
        timeout=20
    )
    resp = r.json()
    if 'access_token' not in resp:
        raise RuntimeError(f"Instagram 토큰 갱신 실패: {resp}")
    days = resp.get('expires_in', 0) // 86400
    print(f"[OK] Instagram 토큰 갱신 완료 (유효기간 {days}일)")
    return resp['access_token']


def refresh_threads() -> str:
    r = requests.get(
        'https://graph.threads.net/refresh_access_token',
        params={'grant_type': 'th_refresh_token', 'access_token': TH_TOKEN},
        timeout=20
    )
    resp = r.json()
    if 'access_token' not in resp:
        raise RuntimeError(f"Threads 토큰 갱신 실패: {resp}")
    days = resp.get('expires_in', 0) // 86400
    print(f"[OK] Threads 토큰 갱신 완료 (유효기간 {days}일)")
    return resp['access_token']


def update_secret(name: str, value: str):
    headers = {'Authorization': f'Bearer {GH_PAT}', 'Accept': 'application/vnd.github+json'}
    key_data = requests.get(
        f'https://api.github.com/repos/{GH_REPO}/actions/secrets/public-key',
        headers=headers, timeout=10
    ).json()

    pk = public.PublicKey(key_data['key'].encode(), encoding.Base64Encoder())
    encrypted = base64.b64encode(public.SealedBox(pk).encrypt(value.encode())).decode()

    r = requests.put(
        f'https://api.github.com/repos/{GH_REPO}/actions/secrets/{name}',
        headers=headers,
        json={'encrypted_value': encrypted, 'key_id': key_data['key_id']},
        timeout=10
    )
    if r.status_code in (201, 204):
        print(f"[OK] GitHub Secret '{name}' 업데이트 완료")
    else:
        raise RuntimeError(f"Secret 업데이트 실패: {r.status_code} {r.text}")


if __name__ == '__main__':
    print("=== 소셜 토큰 갱신 시작 ===")
    new_ig = refresh_instagram()
    new_th = refresh_threads()
    update_secret('IG_ACCESS_TOKEN',      new_ig)
    update_secret('THREADS_ACCESS_TOKEN', new_th)
    print("=== 토큰 갱신 완료 ===")
