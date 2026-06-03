-- 2026-05-30: P2 cross-bot consistency audit
-- Flag rent listings priced >1.5M AED/yr with ≤1 BR or office/studio type
-- as 'needs_review' (likely sale-misclass from extractor).
--
-- Why this specific cohort: high-end multi-BR penthouses/villas (Royal Atlantis
-- penthouse 4.7M, large District One villas) can legitimately rent for >1M AED/yr,
-- so we do NOT touch them. But a 1-bedroom for >1.5M AED/yr is almost certainly
-- a sale price miscategorized as rent.
--
-- This script does not deactivate — listings stay visible but flagged for human
-- review via status='needs_review'. Run UPDATE then SELECT to verify.

BEGIN;

-- Backup affected rows for audit_log
INSERT INTO audit_log (event_type, payload, created_at)
SELECT
  'rent_sale_misclass_flag',
  jsonb_build_object(
    'listing_id', id,
    'area', area,
    'building', building,
    'price', price,
    'bedrooms', bedrooms,
    'property_type', property_type,
    'prior_status', status
  ),
  now()
FROM public.listings
WHERE is_active = true
  AND deal_type = 'rent'
  AND price > 1500000
  AND (COALESCE(bedrooms, 0) <= 1 OR property_type IN ('office', 'studio'));

UPDATE public.listings
SET status = 'needs_review',
    audit_reason = COALESCE(audit_reason, '') ||
                   ' [2026-05-30 rent>1.5M with ≤1BR/office/studio — likely sale-misclass]',
    audit_flagged_at = now()
WHERE is_active = true
  AND deal_type = 'rent'
  AND price > 1500000
  AND (COALESCE(bedrooms, 0) <= 1 OR property_type IN ('office', 'studio'));

-- Verify
SELECT COUNT(*) AS flagged FROM public.listings
WHERE status = 'needs_review'
  AND audit_flagged_at >= now() - interval '1 minute';

COMMIT;
