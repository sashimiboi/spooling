"""Local embeddings via sentence-transformers."""

from functools import lru_cache

from spooling.config import EMBEDDING_MODEL, CHUNK_SIZE


@lru_cache(maxsize=1)
def _get_model():
    """Lazy-load the embedding model."""
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(EMBEDDING_MODEL)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts. Returns list of vectors."""
    model = _get_model()
    embeddings = model.encode(texts, show_progress_bar=False, normalize_embeddings=True)
    return [e.tolist() for e in embeddings]


def embed_text(text: str) -> list[float]:
    """Embed a single text."""
    return embed_texts([text])[0]


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE) -> list[str]:
    """Split text into chunks for embedding."""
    if len(text) <= chunk_size:
        return [text] if text.strip() else []

    chunks = []
    # Split on paragraph boundaries first, then by size
    paragraphs = text.split("\n\n")
    current = ""

    for para in paragraphs:
        if len(current) + len(para) + 2 <= chunk_size:
            current = f"{current}\n\n{para}" if current else para
        else:
            if current.strip():
                chunks.append(current.strip())
            # If a single paragraph exceeds chunk_size, split by sentences
            if len(para) > chunk_size:
                words = para.split()
                current = ""
                for word in words:
                    if len(current) + len(word) + 1 <= chunk_size:
                        current = f"{current} {word}" if current else word
                    else:
                        if current.strip():
                            chunks.append(current.strip())
                        current = word
            else:
                current = para

    if current.strip():
        chunks.append(current.strip())

    return chunks
