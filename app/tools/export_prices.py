"""Refresh the committed price seed from locally saved prices: uv run python -m app.tools.export_prices"""
from app.chargers.knowledge_store import ChargerKnowledgeStore
from app.config.settings import settings

if __name__ == "__main__":
    count = ChargerKnowledgeStore(settings.charger_knowledge_path).export_price_seed(settings.price_seed_path)
    print(f"Wrote {count} Supercharger prices to {settings.price_seed_path}")
