async def _execute_dag_run(run_id, task_id, org_id, dag_domain, dag_root_intent, context, trace_mode=False):
    """Execute a DAG run claimed from XIOSYNC."""
    import httpx as _httpx
    logger.info(f"dag_run.start: run_id={run_id} domain={dag_domain} root={dag_root_intent}")
    
    _XIOSYNC = os.environ.get('XIORUN_XIOSYNC_BASE', os.environ.get('XIOSYNC_BASE', ''))
    _INTERNAL = os.environ.get('XIORUN_INTERNAL_SECRET', '')
    _HEADERS = {'X-XIOSYNC-Internal': _INTERNAL}
    _PROXY_LOCAL_PORT = 19056
    
    try:
        # 1. Fetch memory graph
        async with _httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{_XIOSYNC}/api/v1/xioflow/events/memory-graph-internal",
                params={'domain': dag_domain, 'intent': dag_root_intent},
                headers=_HEADERS
            )
            if resp.status_code != 200:
                raise RuntimeError(f"Failed to fetch graph: {resp.status_code} {resp.text}")
            graph = resp.json()  # {nodes: [{intent, action_type, action_params, place_value, face_value, locator_priority, next_intents}]}
        
        # 2. Launch Patchright
        from patchright.async_api import async_playwright as _pw
        workflow_vars = context.get('workflow_vars', {})
        proxy_url = f"socks5://127.0.0.1:{_PROXY_LOCAL_PORT}" if _PROXY_LOCAL_PORT else None
        
        async with _pw() as pw:
            browser = await pw.chromium.launch(
                headless=False,
                args=[
                    '--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
                    '--window-size=1920,1080', '--disable-blink-features=AutomationControlled',
                    '--lang=en-US',
                ] + ([f'--proxy-server={proxy_url}'] if proxy_url else []),
            )
            
            bctx = await browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
                locale='en-US',
            )
            page = await bctx.new_page()
            
            # 3. Execute nodes in order
            TIER_MAP = {
                1: lambda pv: f'[data-testid="{pv["test_id"]}"]' if pv.get('test_id') else None,
                2: lambda pv: f'[aria-label="{pv["aria_label"]}"]' if pv.get('aria_label') else None,
                3: lambda pv: pv.get('axes_xpath'),
                4: lambda pv: f'text="{pv["inner_text"]}"' if pv.get('inner_text') else None,
                5: lambda pv: f'xpath={pv["xpath"]}' if pv.get('xpath') else None,
                6: lambda pv: pv.get('css'),
            }
            
            nodes = graph.get('nodes', [])
            results = []
            all_ok = True
            
            for node in nodes:
                intent = node['intent']
                action = node['action_type']
                params = node.get('action_params', {})
                pv = node.get('place_value', {}) or {}
                priority = node.get('locator_priority', [6,1,2,3,4,5])
                
                step_ok = False
                try:
                    if action == 'navigate':
                        url = params.get('url', '')
                        # Substitute workflow_vars into URL
                        for k, v in workflow_vars.items():
                            url = url.replace(f'{{{k}}}', str(v))
                        await page.goto(url, wait_until='domcontentloaded', timeout=30000)
                        await page.wait_for_timeout(2000)
                        step_ok = True
                    elif action in ('fill', 'type'):
                        text = params.get('text', '')
                        for k, v in workflow_vars.items():
                            text = text.replace(f'{{{k}}}', str(v))
                        for t in priority:
                            fn = TIER_MAP.get(t)
                            if not fn: continue
                            loc_str = fn(pv)
                            if not loc_str: continue
                            try:
                                loc = page.locator(loc_str)
                                if await loc.count() == 0: continue
                                await loc.first.fill(text, timeout=8000)
                                step_ok = True; break
                            except Exception: continue
                    elif action == 'click':
                        for t in priority:
                            fn = TIER_MAP.get(t)
                            if not fn: continue
                            loc_str = fn(pv)
                            if not loc_str: continue
                            try:
                                loc = page.locator(loc_str)
                                if await loc.count() == 0: continue
                                await loc.first.click(timeout=8000)
                                step_ok = True; break
                            except Exception: continue
                    elif action == 'wait':
                        ms = params.get('ms', 2000)
                        await page.wait_for_timeout(ms)
                        step_ok = True
                    else:
                        logger.warning(f'dag_run: unknown action {action!r} for {intent!r}')
                        step_ok = True  # skip unknown actions
                    
                    if not step_ok:
                        logger.warning(f'dag_run: all locators failed for {intent!r}')
                        all_ok = False
                        break
                    else:
                        logger.info(f'dag_run.step_ok: {intent} [{action}]')
                except Exception as e:
                    logger.error(f'dag_run.step_error: {intent} [{action}] {e}')
                    all_ok = False; break
                
                results.append({'intent': intent, 'ok': step_ok})
                await page.wait_for_timeout(1500)
            
            await browser.close()
        
        # 4. Report back
        async with _httpx.AsyncClient(timeout=30) as client:
            await client.post(
                f"{_XIOSYNC}/api/v1/xioflow/events/runs-internal/{run_id}/complete",
                headers=_HEADERS,
                json={'success': all_ok, 'task_id': task_id, 'result': {'steps': results}}
            )
        logger.info(f"dag_run.done: run_id={run_id} ok={all_ok}")
    
    except Exception as e:
        logger.error(f"dag_run.fatal: run_id={run_id} {e}")
        try:
            async with _httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"{_XIOSYNC}/api/v1/xioflow/events/runs-internal/{run_id}/complete",
                    headers=_HEADERS,
                    json={'success': False, 'task_id': task_id, 'error': str(e)}
                )
        except Exception: pass

async def _dag_poll_loop():
    """Background task — poll XIOSYNC for pending DAG runs every 5s."""
    import httpx as _httpx
    _XIOSYNC = os.environ.get('XIORUN_XIOSYNC_BASE', os.environ.get('XIOSYNC_BASE', ''))
    _INTERNAL = os.environ.get('XIORUN_INTERNAL_SECRET', '')
    _active_runs: set = set()
    
    if not _XIOSYNC or not _INTERNAL:
        logger.warning('dag_poll: XIORUN_XIOSYNC_BASE or XIORUN_INTERNAL_SECRET not set — polling disabled')
        return
    
    logger.info('dag_poll: starting polling loop')
    while True:
        await asyncio.sleep(5)
        try:
            async with _httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f'{_XIOSYNC}/api/v1/xioflow/events/runs/pending-dag-internal',
                    headers={'X-XIOSYNC-Internal': _INTERNAL}
                )
            if resp.status_code == 204:
                continue  # nothing pending
            if resp.status_code != 200:
                logger.warning(f'dag_poll: unexpected status {resp.status_code}')
                continue
            run = resp.json()
            run_id = run.get('run_id')
            if not run_id or run_id in _active_runs:
                continue
            _active_runs.add(run_id)
            logger.info(f'dag_poll: claimed run {run_id} domain={run.get("dag_domain")} intent={run.get("dag_root_intent")}')
            asyncio.create_task(_execute_dag_run(
                run_id=run_id,
                task_id=run.get('task_id'),
                org_id=run.get('organization_id'),
                dag_domain=run.get('dag_domain'),
                dag_root_intent=run.get('dag_root_intent'),
                context=run.get('context', {}),
                trace_mode=run.get('context', {}).get('trace_mode', False),
            ))
            _active_runs.discard(run_id)  # clean up after task fires
        except Exception as e:
            logger.debug(f'dag_poll: error {e}')
