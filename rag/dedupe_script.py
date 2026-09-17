import asyncio
from collections import defaultdict
from rag.retrieval.vector_store import get_qdrant_client
from rag.config import rag_settings

async def dedupe():
    client = get_qdrant_client()
    name = rag_settings.qdrant_collection
    result = await client.scroll(collection_name=name, limit=200, with_payload=True)
    points = result[0]
    groups = defaultdict(list)
    for p in points:
        key = (p.payload.get('parent_source'), p.payload.get('chunk_index'))
        groups[key].append(p.id)
    to_delete = []
    for key, ids in groups.items():
        if len(ids) > 1:
            to_delete.extend(ids[1:])
    print(f'Total points: {len(points)} | Unique groups: {len(groups)} | Deleting: {len(to_delete)}')
    if to_delete:
        await client.delete(collection_name=name, points_selector=to_delete)
    print('Done.')

asyncio.run(dedupe())