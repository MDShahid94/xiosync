WITH org AS (
    SELECT id FROM organizations LIMIT 1
),
tmpl AS (
    SELECT id, slug, dag_domain, dag_root_intent, template_type, config 
    FROM workflow_templates 
    WHERE slug = 'google-signin-dag' LIMIT 1
)
INSERT INTO xioflow_runs
  (id, organization_id, template_id, state, context, template_snapshot, started_at)
SELECT 
  gen_random_uuid(),
  org.id,
  tmpl.id,
  'PENDING',
  '{"email": "evanarose.com@gmail.com", "password": "TheTruth1!", "totp_secret": "wdkpyermwk3o5uwetcck5gbhltprowkq"}'::jsonb,
  jsonb_build_object(
      'slug', tmpl.slug,
      'dag_domain', tmpl.dag_domain,
      'dag_root_intent', tmpl.dag_root_intent,
      'template_type', tmpl.template_type,
      'config', tmpl.config
  ),
  now()
FROM org, tmpl
RETURNING id;
