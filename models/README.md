# Local ML weights (not committed)

These folders are filled on first run / setup via Hugging Face.

A source checkout still uses `models/embeddings/all-MiniLM-L6-v2/` in this repository. Packaged builds (including the Linux AppImage and MCP clients) keep one stable copy under the app userData folder, for example:

- Linux: `~/.config/Clippy Vision/models/embeddings/all-MiniLM-L6-v2/`
- macOS: `~/Library/Application Support/Clippy Vision/models/embeddings/all-MiniLM-L6-v2/`
- Windows: `%APPDATA%\Clippy Vision\models\embeddings\all-MiniLM-L6-v2\`

`CLIPPY_EMBEDDING_MODEL_DIR` overrides that location. A leftover copy inside the code folder or an old `mcp-runtime-*` directory is moved or copied once.

| Path | Source |
|------|--------|
| `embeddings/all-MiniLM-L6-v2/` | [`sentence-transformers/all-MiniLM-L6-v2`](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2) |

```powershell
# from the repo root
python -m core.model_download
```
