"""
FoCo Fuel — AI plate composition (Gemini), kept separate from the rules layer.

This module ONLY talks to the model. It never sees the user's identity, their
allergens, or their preferences — the caller passes in a pre-filtered shortlist
(already safe) plus today's training load, and nothing else. The API key stays
here on the server and is never returned to the caller's output.

`generate_plates` raises on any problem (missing key, timeout, HTTP error, bad
JSON). The server treats every exception as "use the rules fallback".
"""

import json
import ssl
import os
import urllib.request

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _ssl_context():
    """Same cert fix as the menu fetch (python.org build lacks a CA bundle)."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    if os.path.exists("/etc/ssl/cert.pem"):
        return ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    return ssl.create_default_context()


_SSL_CTX = _ssl_context()


def build_prompt(shortlist, demand):
    """The full instruction sent to the model. Only shortlist + load — no PII."""
    return (
        "You help a Dartmouth student-athlete assemble a fueling plate from "
        "today's dining-hall menu.\n\n"
        f"Today's training load: {demand}.\n\n"
        "Here are the ONLY items you may use. They are already filtered to be "
        "safe for this person. Each has an id, a name, a role tag "
        "(protein / carb / veg / other), and per-portion macros in grams "
        "(null means the value is unknown — do not treat it as zero):\n\n"
        f"{json.dumps(shortlist, ensure_ascii=False)}\n\n"
        "Compose 2 to 3 complete plates that a person would actually eat "
        "together. Rules:\n"
        "- Use ONLY items from the list above, referenced by their EXACT id. "
        "Never invent an item or an id.\n"
        "- Anchor every plate with a protein item.\n"
        "- Always include at least one vegetable (role \"veg\").\n"
        "- Scale carbohydrate emphasis to the training load: a heavier load "
        "means more substantial carbs; an easy or rest day means lighter on "
        "carbs.\n"
        "- Give a one-line rationale per plate tied to today's load, in terms "
        "of fueling and energy — never dieting, weight, or restriction. Do NOT "
        "give the person calorie or gram targets.\n"
        "- Plates must be sensible, realistic combinations.\n\n"
        "Return STRICT JSON in exactly this shape and nothing else:\n"
        '{"plates":[{"items":["id1","id2"],"why":"..."}]}'
    )


def _call_gemini(prompt, api_key, model, timeout):
    """POST to Gemini and return the raw text of the first candidate."""
    url = ENDPOINT.format(model=model)
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.4,
            "responseMimeType": "application/json",
        },
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,   # key travels in a header, not the URL
        },
    )
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        data = json.load(resp)
    # Dig out the model's text output.
    candidates = data.get("candidates") or []
    if not candidates:
        raise ValueError("Gemini returned no candidates")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise ValueError("Gemini returned empty text")
    return text


def generate_plates(shortlist, demand, api_key, model, timeout=20):
    """Ask Gemini for plates. Returns a list of {items:[id...], why:str}.

    Raises on any failure so the caller can fall back to rules. Does NOT
    validate that ids exist in the shortlist — that check lives in the server,
    right before rendering.
    """
    if not api_key:
        raise ValueError("no GEMINI_API_KEY configured")
    if not model:
        raise ValueError("no GEMINI_MODEL configured")

    prompt = build_prompt(shortlist, demand)
    text = _call_gemini(prompt, api_key, model, timeout)

    # responseMimeType asks for JSON, but be forgiving of stray wrapping.
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("Gemini response was not JSON")
        parsed = json.loads(text[start:end + 1])

    plates = parsed.get("plates")
    if not isinstance(plates, list):
        raise ValueError("Gemini JSON missing 'plates' list")

    out = []
    for p in plates:
        items = p.get("items") if isinstance(p, dict) else None
        if isinstance(items, list) and items:
            out.append({
                "items": [str(i) for i in items],
                "why": (p.get("why") or "").strip(),
            })
    if not out:
        raise ValueError("Gemini JSON had no usable plates")
    return out
