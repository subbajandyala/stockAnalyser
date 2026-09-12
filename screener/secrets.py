"""
Load secrets from Google Secret Manager when running on Cloud Run,
fall back to Streamlit secrets / env vars locally.
"""

import os


def _from_secret_manager(name: str) -> str:
    try:
        from google.cloud import secretmanager
        project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
        if not project:
            return ""
        client = secretmanager.SecretManagerServiceClient()
        resource = f"projects/{project}/secrets/{name}/versions/latest"
        resp = client.access_secret_version(request={"name": resource})
        return resp.payload.data.decode("utf-8").strip()
    except Exception:
        return ""


def get_secret(name: str, streamlit_key: str | None = None) -> str:
    """
    Resolve a secret in priority order:
      1. Environment variable (e.g. KITE_API_KEY)
      2. Streamlit secrets (st.secrets)
      3. Google Secret Manager
    """
    # 1. env var
    val = os.environ.get(name, "")
    if val:
        return val

    # 2. Streamlit secrets
    if streamlit_key:
        try:
            import streamlit as st
            val = st.secrets.get(streamlit_key, "")
            if val:
                return val
        except Exception:
            pass

    # 3. Secret Manager
    return _from_secret_manager(name)
