from shared.market_world_model import _db
with _db.connect() as c:
    cur = c.cursor()
    queries = [
        ('SELECT COUNT(*) FROM market_entities', 'entities total'),
        ("SELECT entity_type, COUNT(*) FROM market_entities GROUP BY entity_type ORDER BY 1", 'by type'),
        ('SELECT COUNT(*) FROM market_state_snapshots', 'snapshots'),
        ('SELECT COUNT(*) FROM market_relationships', 'relationships'),
        ('SELECT COUNT(*) FROM market_models', 'models'),
        ("SELECT algo, COUNT(*) FROM market_models GROUP BY algo ORDER BY 1", 'models by algo'),
        ("SELECT entity_type, COUNT(*) FROM market_entities e JOIN market_models m ON m.entity_id=e.id GROUP BY 1 ORDER BY 1", 'models by entity_type'),
    ]
    for q, label in queries:
        cur.execute(q)
        print(f'{label}: {cur.fetchall()}')
