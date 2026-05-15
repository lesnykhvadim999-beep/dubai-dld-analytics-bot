execute_values(
        cur,
        """
        INSERT INTO dld_transactions_full (
            transaction_id,
            transaction_number,
            transaction_date,
            procedure_name,
            area_id,
            area_en,
            area_ar,
            project_en,
            project_ar,
            building_en,
            building_ar,
            prop_type_en,
            prop_sub_type_en,
            rooms_en,
            actual_worth,
            meter_sale_price,
            actual_area,
            procedure_area,
            parking,
            nearest_metro_en,
            nearest_mall_en,
            nearest_landmark_en,
            usage_id,
            is_free_hold,
            is_offplan
        )
        VALUES %s
        ON CONFLICT (transaction_id) DO NOTHING
        """,
        values
   )

    conn.commit()


def run_parser(from_date, to_date):
    skip = 0
    take = 1000
    total = 0

    while True:
        print(f"Fetching skip={skip}")

        data = fetch_transactions(
            from_date=from_date,
            to_date=to_date,
            skip=skip,
            take=take
        )

        rows = extract_rows(data)

        print(f"Received: {len(rows)}")

        if not rows:
            print("Finished.")
            break

        save_transactions(rows)

        total += len(rows)
        print(f"Saved total: {total}")

        if len(rows) < take:
            print("Last page reached.")
            break

        skip += take
        time.sleep(1)

    cur.close()
    conn.close()

    print("DONE")


if name == "main":
    run_parser(
        from_date="05/01/2026",
        to_date="05/15/2026"
    )
