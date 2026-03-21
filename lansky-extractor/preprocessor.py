import logging
import re

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_HEADER_KEYWORDS = re.compile(r"compra|pago|transferencia", re.IGNORECASE)

_KNOWN_LABELS = [
    "Monto",
    "Fecha",
    "Hora",
    "Comercio",
    "Cuotas",
    "Número tarjeta",
    "Cuenta de origen",
    "Tarjeta de crédito",
    "Nombre del destinatario",
    "Banco de destino",
    "Cuenta de destino",
    "Mensaje",
    "Número de comprobante",
    "Nº de operación",
]

_KNOWN_LABELS_PATTERN = re.compile(
    r"(" + "|".join(re.escape(l) for l in _KNOWN_LABELS) + r")\s*\n([^\n]+)",
    re.IGNORECASE,
)


def preprocess(html_body: str) -> str:
    soup = BeautifulSoup(html_body, "html.parser")

    header_phrase = _extract_header(soup)
    pairs = _extract_table_pairs(soup)

    if len(pairs) < 2:
        pairs = _extract_text_pairs(soup)

    log.debug("header_phrase=%r pairs_found=%d", header_phrase, len(pairs))

    if not header_phrase and not pairs:
        return soup.get_text(separator="\n", strip=True)[:2000]

    lines = []
    if header_phrase:
        lines.append(f"Tipo: {header_phrase}")
    for label, value in pairs:
        lines.append(f"{label}: {value}")

    return "\n".join(lines)


def _extract_header(soup: BeautifulSoup) -> str:
    for text in soup.stripped_strings:
        clean = " ".join(text.split())
        if _HEADER_KEYWORDS.search(clean):
            return clean
    return ""


def _extract_table_pairs(soup: BeautifulSoup) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) == 2:
                label = " ".join(cells[0].get_text().split())
                value = " ".join(cells[1].get_text().split())
                if label and value:
                    pairs.append((label, value))
    return pairs


def _extract_text_pairs(soup: BeautifulSoup) -> list[tuple[str, str]]:
    text = soup.get_text(separator="\n", strip=True)
    pairs: list[tuple[str, str]] = []
    seen_labels: set[str] = set()
    for match in _KNOWN_LABELS_PATTERN.finditer(text):
        label = match.group(1).strip()
        value = match.group(2).strip()
        if label not in seen_labels and value:
            pairs.append((label, value))
            seen_labels.add(label)
    return pairs
