import os
import psycopg2

DATABASE_URL = os.getenv('DATABASE_URL') or os.getenv('SALE_DATABASE_URL') or os.getenv('RENT_DATABASE_URL')
if not DATABASE_URL:
    raise RuntimeError('No DB URL. Set DATABASE_URL / SALE_DATABASE_URL / RENT_DATABASE_URL')


def qident(name):
    return '"' + name.replace('"', '""') + '"'


def table_exists(cur, table):
    cur.execute("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema='public' AND table_name=%s
        )
    """, (table,))
    return cur.fetchone()[0]


def cols(cur, table):
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s
    """, (table,))
    return {r[0] for r in cur.fetchall()}


def has_col(cur, table, col):
    return col in cols(cur, table)


def add_col(cur, table, col, typ):
    if not has_col(cur, table, col):
        print(f'ADD COLUMN {table}.{col}')
        cur.execute(f'ALTER TABLE {qident(table)} ADD COLUMN {qident(col)} {typ}')


def text_expr(existing, candidates):
    parts = [f"NULLIF({qident(c)}::text, '')" for c in candidates if c in existing]
    return 'COALESCE(' + ', '.join(parts) + ')' if parts else 'NULL'


def num_expr(existing, candidates):
    parts = [f"NULLIF({qident(c)}::text, '')::numeric" for c in candidates if c in existing]
    return 'COALESCE(' + ', '.join(parts) + ')' if parts else 'NULL'


def room_norm(expr):
    return f"""
    CASE
      WHEN {expr} IS NULL THEN NULL
      WHEN lower({expr}) LIKE '%studio%' THEN 'Studio'
      WHEN lower({expr}) LIKE '%5%' OR lower({expr}) LIKE '%6%' OR lower({expr}) LIKE '%7%' THEN '5 BR+'
      WHEN lower({expr}) LIKE '%4%' THEN '4 BR'
      WHEN lower({expr}) LIKE '%3%' THEN '3 BR'
      WHEN lower({expr}) LIKE '%2%' THEN '2 BR'
      WHEN lower({expr}) LIKE '%1%' THEN '1 BR'
      WHEN lower({expr}) LIKE '%villa%' OR lower({expr}) LIKE '%townhouse%' THEN 'Villa/Townhouse'
      ELSE {expr}
    END
    """


def fix_table(cur, table, kind):
    if not table_exists(cur, table):
        print(f'SKIP: {table} not found')
        return False

    for col, typ in [
        ('building_name', 'TEXT'), ('area_name', 'TEXT'), ('project_name', 'TEXT'),
        ('property_type', 'TEXT'), ('property_sub_type', 'TEXT'), ('rooms_norm', 'TEXT'),
        ('price_value', 'NUMERIC'), ('deal_date', 'TIMESTAMP'), ('source_type', 'TEXT')
    ]:
        add_col(cur, table, col, typ)

    c = cols(cur, table)

    building = text_expr(c, ['building_en','building_name_en','building','property_name_en','project_en','project_name','master_project_en'])
    area = text_expr(c, ['area_en','area_name_en','area','master_project_en','nearest_landmark_en'])
    project = text_expr(c, ['project_en','project_name_en','project','building_en','building_name','master_project_en'])
    ptype = text_expr(c, ['prop_type_en','property_type_en','type'])
    subtype = text_expr(c, ['prop_sub_type_en','prop_sb_type_en','property_sub_type_en','rooms_en','rooms'])
    rooms_src = text_expr(c, ['rooms_en','rooms','room_type','prop_sub_type_en','prop_sb_type_en','property_sub_type'])

    if kind == 'sale':
        price = num_expr(c, ['actual_worth','transaction_amount','amount','value','price','meter_sale_price'])
        date = text_expr(c, ['transaction_date','instance_date','procedure_date','date'])
    else:
        price = num_expr(c, ['annual_amount','contract_amount','rent_amount','amount','value'])
        date = text_expr(c, ['registration_date','transaction_date','date'])

    rn = room_norm(rooms_src)

    print(f'Updating compatibility fields in {table}...')
    cur.execute(f"""
        UPDATE {qident(table)}
        SET
            building_name = COALESCE(NULLIF(building_name, ''), {building}),
            area_name = COALESCE(NULLIF(area_name, ''), {area}),
            project_name = COALESCE(NULLIF(project_name, ''), {project}),
            property_type = COALESCE(NULLIF(property_type, ''), {ptype}),
            property_sub_type = COALESCE(NULLIF(property_sub_type, ''), {subtype}),
            rooms_norm = COALESCE(NULLIF(rooms_norm, ''), {rn}),
            price_value = COALESCE(price_value, {price}),
            deal_date = COALESCE(deal_date, NULLIF(({date})::text, '')::timestamp),
            source_type = COALESCE(NULLIF(source_type, ''), %s)
        WHERE
            building_name IS NULL OR building_name = '' OR
            area_name IS NULL OR area_name = '' OR
            project_name IS NULL OR project_name = '' OR
            property_type IS NULL OR property_type = '' OR
            property_sub_type IS NULL OR property_sub_type = '' OR
            rooms_norm IS NULL OR rooms_norm = '' OR
            price_value IS NULL OR deal_date IS NULL OR
            source_type IS NULL OR source_type = ''
    """, (kind,))
    print(f'{table}: updated {cur.rowcount} rows')
    return True


def create_views(cur):
    sales = table_exists(cur, 'dld_transactions_full')
    rents = table_exists(cur, 'dld_rents_full')
    if sales:
        id_col = 'transaction_id' if has_col(cur, 'dld_transactions_full', 'transaction_id') else None
        num_col = 'transaction_number' if has_col(cur, 'dld_transactions_full', 'transaction_number') else None
        raw_col = 'raw_json' if has_col(cur, 'dld_transactions_full', 'raw_json') else None
        actual_area = 'actual_area' if has_col(cur, 'dld_transactions_full', 'actual_area') else None
        cur.execute(f"""
        CREATE OR REPLACE VIEW dld_sales_unified AS
        SELECT
            {qident(id_col)+'::text' if id_col else 'NULL::text'} AS deal_id,
            {qident(num_col)+'::text' if num_col else 'NULL::text'} AS deal_number,
            'sale'::text AS deal_type,
            deal_date, area_name, building_name, project_name,
            property_type, property_sub_type, rooms_norm, price_value,
            {qident(actual_area) if actual_area else 'NULL::numeric'} AS actual_area,
            {qident(raw_col) if raw_col else 'NULL::jsonb'} AS raw_json
        FROM dld_transactions_full
        """)
    if rents:
        id_col = 'rent_id' if has_col(cur, 'dld_rents_full', 'rent_id') else None
        raw_col = 'raw_json' if has_col(cur, 'dld_rents_full', 'raw_json') else None
        actual_area = 'actual_area' if has_col(cur, 'dld_rents_full', 'actual_area') else None
        cur.execute(f"""
        CREATE OR REPLACE VIEW dld_rents_unified AS
        SELECT
            {qident(id_col)+'::text' if id_col else 'NULL::text'} AS deal_id,
            NULL::text AS deal_number,
            'rent'::text AS deal_type,
            deal_date, area_name, building_name, project_name,
            property_type, property_sub_type, rooms_norm, price_value,
            {qident(actual_area) if actual_area else 'NULL::numeric'} AS actual_area,
            {qident(raw_col) if raw_col else 'NULL::jsonb'} AS raw_json
        FROM dld_rents_full
        """)
    if sales and rents:
        cur.execute("""
        CREATE OR REPLACE VIEW dld_all_deals_unified AS
        SELECT * FROM dld_sales_unified
        UNION ALL
        SELECT * FROM dld_rents_unified
        """)
    elif sales:
        cur.execute("CREATE OR REPLACE VIEW dld_all_deals_unified AS SELECT * FROM dld_sales_unified")
    elif rents:
        cur.execute("CREATE OR REPLACE VIEW dld_all_deals_unified AS SELECT * FROM dld_rents_unified")


def indexes(cur):
    for table in ['dld_transactions_full','dld_rents_full']:
        if not table_exists(cur, table):
            continue
        for col in ['building_name','area_name','project_name','rooms_norm','deal_date']:
            if has_col(cur, table, col):
                cur.execute(f'CREATE INDEX IF NOT EXISTS idx_{table}_{col} ON {qident(table)} ({qident(col)})')


def diagnostics(cur):
    print('\nDIAGNOSTICS')
    for table in ['dld_transactions_full','dld_rents_full']:
        if not table_exists(cur, table):
            print(table, 'missing')
            continue
        cur.execute(f'SELECT COUNT(*) FROM {qident(table)}')
        print(table, 'rows:', cur.fetchone()[0])
        for term in ['grande','corner','jvc','marina','business bay']:
            cur.execute(f"""
                SELECT COUNT(*) FROM {qident(table)}
                WHERE COALESCE(building_name,'') ILIKE %s
                   OR COALESCE(project_name,'') ILIKE %s
                   OR COALESCE(area_name,'') ILIKE %s
            """, (f'%{term}%', f'%{term}%', f'%{term}%'))
            print(' ', term, cur.fetchone()[0])


def main():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    try:
        fix_table(cur, 'dld_transactions_full', 'sale')
        fix_table(cur, 'dld_rents_full', 'rent')
        create_views(cur)
        indexes(cur)
        conn.commit()
        diagnostics(cur)
        print('SUCCESS: compatibility fix completed')
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()

if __name__ == '__main__':
    main()
