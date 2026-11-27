"""Marca do projeto, lida de um arquivo JSON uma vez na importacao.

Fonte: mcp_server/brand.json. PRECOS_BRAND_FILE aponta para outro arquivo (prova de troca de marca).
So biblioteca padrao: o gerador do pacote publico e o OpenAPI importam este modulo sem pydantic.
"""
import json
import os
from types import MappingProxyType

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brand.json")

REQUIRED = (
    "name", "slug", "title", "tagline", "description_short", "site_url", "api_base_url",
    "mcp_url", "demo_mcp_url", "docs_url", "repo_url", "registry_name", "contact_url", "issues_url",
    "data_license", "attribution", "facts",
)
FACTS_REQUIRED = ("stores", "states", "as_of")
# Gerados por scripts/vitrine/update_facts.py. Opcionais por ora (brand.json ainda sem eles); se vierem, sao contagens.
FACTS_GERADOS = ("chains", "stores_fresh_8d")
MAX_DESCRIPTION = 100


def _load(path: str) -> MappingProxyType:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    for field in REQUIRED:
        if field not in data:
            raise RuntimeError(f"brand: campo obrigatorio ausente em {path}: {field}")
    for field in FACTS_REQUIRED:
        if field not in data["facts"]:
            raise RuntimeError(f"brand: campo obrigatorio ausente em {path}: facts.{field}")
    for field in FACTS_GERADOS:
        if field in data["facts"] and not str(data["facts"][field]).isdigit():
            raise RuntimeError(f"brand: facts.{field} deve ser contagem (digitos) em {path}")
    if len(data["description_short"]) > MAX_DESCRIPTION:
        raise RuntimeError(f"brand: description_short passa de {MAX_DESCRIPTION} caracteres")
    data["facts"] = MappingProxyType(dict(data["facts"]))
    return MappingProxyType(data)


BRAND = _load(os.environ.get("PRECOS_BRAND_FILE") or DEFAULT_PATH)
