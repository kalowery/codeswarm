const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const fsp = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const net = require('node:net')
const { spawn, spawnSync } = require('node:child_process')
const puppeteer = require('puppeteer')

const ROOT = path.resolve(__dirname, '..', '..')
const TEST_TIMEOUT_MS = 420_000

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, 'utf8'))
}

async function mkdtemp(prefix) {
  return await fsp.mkdtemp(path.join(os.tmpdir(), prefix))
}

async function getFreePort() {
  return await new Promise((resolve, reject) => {
    const server = net.createServer()
    server.listen(0, '127.0.0.1', () => {
      const address = server.address()
      const port = typeof address === 'object' && address ? address.port : 0
      server.close((err) => {
        if (err) reject(err)
        else resolve(port)
      })
    })
    server.on('error', reject)
  })
}

function splitLines(buffer, chunk, pushLine) {
  const combined = buffer + chunk
  const lines = combined.split(/\r?\n/)
  const nextBuffer = lines.pop() || ''
  for (const line of lines) {
    if (line.trim()) pushLine(line)
  }
  return nextBuffer
}

function spawnLogged(name, command, args, options = {}) {
  const logs = []
  const child = spawn(command, args, {
    cwd: ROOT,
    env: options.env,
    stdio: ['ignore', 'pipe', 'pipe'],
    detached: true
  })

  const pushLog = (stream, line) => {
    const entry = `[${name} ${stream}] ${line}`
    logs.push(entry)
    if (logs.length > 300) logs.shift()
  }

  let stdoutBuffer = ''
  let stderrBuffer = ''
  child.stdout.setEncoding('utf8')
  child.stderr.setEncoding('utf8')
  child.stdout.on('data', (chunk) => {
    stdoutBuffer = splitLines(stdoutBuffer, chunk, (line) => pushLog('stdout', line))
  })
  child.stderr.on('data', (chunk) => {
    stderrBuffer = splitLines(stderrBuffer, chunk, (line) => pushLog('stderr', line))
  })

  const exited = new Promise((resolve) => {
    child.once('exit', (code, signal) => resolve({ code, signal }))
  })

  return {
    name,
    child,
    logs,
    exited,
    async stop() {
      if (child.exitCode !== null || child.signalCode !== null) {
        await exited
        return
      }
      try {
        process.kill(-child.pid, 'SIGTERM')
      } catch {}
      const soft = await Promise.race([
        exited.then(() => true),
        new Promise((resolve) => setTimeout(() => resolve(false), 5000))
      ])
      if (soft) return
      try {
        process.kill(-child.pid, 'SIGKILL')
      } catch {}
      await Promise.race([
        exited,
        new Promise((resolve) => setTimeout(resolve, 2000))
      ])
    }
  }
}

async function waitForPort(port, timeoutMs) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const open = await new Promise((resolve) => {
      const socket = net.createConnection({ host: '127.0.0.1', port })
      socket.once('connect', () => {
        socket.destroy()
        resolve(true)
      })
      socket.once('error', () => resolve(false))
    })
    if (open) return
    await new Promise((resolve) => setTimeout(resolve, 200))
  }
  throw new Error(`Timed out waiting for port ${port}`)
}

async function waitForHttp(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const res = await fetch(url)
      if (res.ok) return
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 300))
  }
  throw new Error(`Timed out waiting for HTTP ${url}`)
}

async function startStack() {
  const tempRoot = await mkdtemp('codeswarm-ui-test-')
  const frontendPort = await getFreePort()
  const backendPort = await getFreePort()
  const routerPort = await getFreePort()
  const backendOrigin = `http://127.0.0.1:${backendPort}`
  const frontendOrigin = `http://127.0.0.1:${frontendPort}`
  const routerStateFile = path.join(tempRoot, 'router-state.json')
  const routerPidFile = path.join(tempRoot, 'router.pid')
  const archiveRoot = path.join(tempRoot, 'archives')
  const workspaceRoot = path.join(tempRoot, 'runs')

  const config = readJson(path.join(ROOT, 'configs', 'local.json'))
  config.cluster = {
    ...(config.cluster || {}),
    workspace_root: workspaceRoot,
    archive_root: archiveRoot
  }
  const configPath = path.join(tempRoot, 'local.test.json')
  await fsp.writeFile(configPath, JSON.stringify(config, null, 2), 'utf8')

  const commonEnv = {
    ...process.env,
    CODESWARM_DISABLE_BEADS_SYNC: '1',
    CODESWARM_ROUTER_HOST: '127.0.0.1',
    CODESWARM_ROUTER_PORT: String(routerPort)
  }

  const router = spawnLogged('router', 'python3', ['-u', '-m', 'router.router', '--config', configPath, '--daemon'], {
    env: {
      ...commonEnv,
      CODESWARM_ROUTER_PID_FILE: routerPidFile,
      CODESWARM_ROUTER_STATE_FILE: routerStateFile
    }
  })
  await waitForPort(routerPort, 20_000)

  const backend = spawnLogged('backend', 'npm', ['--workspace=backend', 'run', 'web'], {
    env: {
      ...commonEnv,
      CODESWARM_WEB_BACKEND_PORT: String(backendPort)
    }
  })
  await waitForHttp(`${backendOrigin}/swarms`, 30_000)

  const frontend = spawnLogged('frontend', 'npm', ['--workspace=frontend', 'run', 'dev'], {
    env: {
      ...process.env,
      PORT: String(frontendPort),
      NEXT_PUBLIC_CODESWARM_BACKEND_ORIGIN: backendOrigin
    }
  })
  await waitForHttp(frontendOrigin, 60_000)

  return {
    tempRoot,
    frontendOrigin,
    backendOrigin,
    routerStateFile,
    configPath,
    processes: [frontend, backend, router],
    async stop() {
      for (const proc of [frontend, backend, router]) {
        await proc.stop()
      }
      await fsp.rm(tempRoot, { recursive: true, force: true })
    },
    dumpLogs() {
      return [router, backend, frontend]
        .flatMap((proc) => proc.logs.slice(-80))
        .join('\n')
    }
  }
}

function runOrThrow(args, cwd) {
  const result = spawnSync(args[0], args.slice(1), {
    cwd,
    encoding: 'utf8'
  })
  if (result.status !== 0) {
    throw new Error(`Command failed (${args.join(' ')}): ${(result.stderr || result.stdout || '').trim()}`)
  }
  return result
}

async function createRepoWithOrigin(name) {
  const baseDir = await mkdtemp(`codeswarm-ui-repo-${name}-`)
  const originDir = path.join(baseDir, 'origin.git')
  const repoDir = path.join(baseDir, 'repo')
  runOrThrow(['git', 'init', '--bare', originDir], ROOT)
  runOrThrow(['git', 'clone', originDir, repoDir], ROOT)
  runOrThrow(['git', '-C', repoDir, 'config', 'user.email', 'ui-smoke@example.test'], ROOT)
  runOrThrow(['git', '-C', repoDir, 'config', 'user.name', 'Codeswarm UI Smoke'], ROOT)
  runOrThrow(['git', '-C', repoDir, 'checkout', '-b', 'main'], ROOT)
  fs.writeFileSync(path.join(repoDir, 'README.md'), `# ${name}\n`, 'utf8')
  runOrThrow(['git', '-C', repoDir, 'add', 'README.md'], ROOT)
  runOrThrow(['git', '-C', repoDir, 'commit', '-m', 'init'], ROOT)
  runOrThrow(['git', '-C', repoDir, 'push', '-u', 'origin', 'main'], ROOT)
  return { baseDir, originDir, repoDir }
}

async function verifyBranchFile(originDir, branch, relPath) {
  const verifyDir = await mkdtemp('codeswarm-ui-verify-')
  try {
    runOrThrow(['git', 'clone', originDir, path.join(verifyDir, 'repo')], ROOT)
    const repoDir = path.join(verifyDir, 'repo')
    runOrThrow(['git', '-C', repoDir, 'fetch', 'origin', branch], ROOT)
    runOrThrow(['git', '-C', repoDir, 'checkout', '-B', branch, `origin/${branch}`], ROOT)
    return await fsp.readFile(path.join(repoDir, relPath), 'utf8')
  } finally {
    await fsp.rm(verifyDir, { recursive: true, force: true })
  }
}

async function backendJson(stack, pathname) {
  const res = await fetch(`${stack.backendOrigin}${pathname}`)
  assert.equal(res.ok, true, `Expected HTTP 2xx for ${pathname}, got ${res.status}`)
  return await res.json()
}

async function waitForProject(stack, title, predicate, timeoutMs = 120_000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const projects = await backendJson(stack, '/projects')
    const project = Object.values(projects).find((item) => item && item.title === title)
    if (project && predicate(project)) return project
    await new Promise((resolve) => setTimeout(resolve, 400))
  }
  throw new Error(`Timed out waiting for project ${title}`)
}

async function waitForSwarmReady(stack, alias, timeoutMs = 60_000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const swarms = await backendJson(stack, '/swarms')
    const swarm = Object.values(swarms).find((item) => item && item.alias === alias)
    if (swarm) {
      const nodes = Object.values(swarm.nodes || {})
      const busy = nodes.some((node) => {
        const turns = Array.isArray(node.turns) ? node.turns : []
        const last = turns[turns.length - 1]
        return last && last.phase !== 'completed' && last.phase !== 'error'
      })
      if (!busy) return swarm
    }
    await new Promise((resolve) => setTimeout(resolve, 400))
  }
  throw new Error(`Timed out waiting for swarm ${alias} to become ready`)
}

async function newPage(browser, stack) {
  const context = browser.createBrowserContext
    ? await browser.createBrowserContext()
    : await browser.createIncognitoBrowserContext()
  const page = await context.newPage()
  page.setDefaultTimeout(30_000)
  page.on('dialog', async (dialog) => {
    throw new Error(`Unexpected dialog: ${dialog.message()}`)
  })
  await page.goto(stack.frontendOrigin, { waitUntil: 'networkidle2' })
  await waitForTestIdText(page, 'ws-status', /connected/i, 30_000)
  return { context, page }
}

function testIdSelector(testId) {
  return `[data-testid="${testId}"]`
}

async function waitForTestId(page, testId, timeoutMs = 30_000) {
  await page.waitForSelector(testIdSelector(testId), { timeout: timeoutMs })
}

async function clickTestId(page, testId) {
  await waitForTestId(page, testId)
  await page.click(testIdSelector(testId))
}

async function setValueByTestId(page, testId, value) {
  await waitForTestId(page, testId)
  await page.$eval(
    testIdSelector(testId),
    (el, nextValue) => {
      el.focus()
      const prototype = Object.getPrototypeOf(el)
      const descriptor = Object.getOwnPropertyDescriptor(prototype, 'value')
      if (descriptor && typeof descriptor.set === 'function') {
        descriptor.set.call(el, String(nextValue))
      } else {
        el.value = String(nextValue)
      }
      el.dispatchEvent(new Event('input', { bubbles: true }))
      el.dispatchEvent(new Event('change', { bubbles: true }))
    },
    value
  )
}

async function setCheckboxByTestId(page, testId, checked) {
  await waitForTestId(page, testId)
  await page.$eval(
    testIdSelector(testId),
    (el, nextChecked) => {
      if (Boolean(el.checked) !== Boolean(nextChecked)) {
        el.click()
      }
    },
    checked
  )
}

async function checkboxStateByTestId(page, testId) {
  await waitForTestId(page, testId)
  return await page.$eval(testIdSelector(testId), (el) => Boolean(el.checked))
}

async function selectOptionByText(page, testId, text) {
  await waitForTestId(page, testId)
  const value = await page.$eval(
    testIdSelector(testId),
    (el, needle) => {
      const option = Array.from(el.options).find((item) => item.textContent.includes(String(needle)))
      return option ? option.value : ''
    },
    text
  )
  assert.ok(value, `Unable to find option text ${text} for ${testId}`)
  await page.select(testIdSelector(testId), value)
}

async function optionStateByText(page, testId, text) {
  await waitForTestId(page, testId)
  const result = await page.$eval(
    testIdSelector(testId),
    (el, needle) => {
      const option = Array.from(el.options).find((item) => (item.textContent || '').includes(String(needle)))
      if (!option) return null
      return {
        value: option.value,
        disabled: Boolean(option.disabled),
        text: option.textContent || ''
      }
    },
    text
  )
  assert.ok(result, `Unable to find option text ${text} for ${testId}`)
  return result
}

async function waitForTestIdText(page, testId, pattern, timeoutMs = 30_000) {
  await page.waitForFunction(
    ([selector, source, flags]) => {
      const el = document.querySelector(selector)
      if (!el) return false
      return new RegExp(source, flags).test(el.textContent || '')
    },
    { timeout: timeoutMs },
    [testIdSelector(testId), pattern.source, pattern.flags]
  )
}

async function waitForCardByText(page, selectorPrefix, text, timeoutMs = 30_000) {
  await page.waitForFunction(
    ([selector, needle]) =>
      Array.from(document.querySelectorAll(selector)).some((el) =>
        (el.textContent || '').includes(String(needle))
      ),
    { timeout: timeoutMs },
    [`[data-testid^="${selectorPrefix}"]`, text]
  )
}

async function clickCardByText(page, selectorPrefix, text) {
  await waitForCardByText(page, selectorPrefix, text)
  const clicked = await page.$$eval(
    `[data-testid^="${selectorPrefix}"]`,
    (els, needle) => {
      const match = els.find((el) => (el.textContent || '').includes(String(needle)))
      if (!match) return false
      match.scrollIntoView({ block: 'center' })
      match.click()
      return true
    },
    text
  )
  assert.equal(clicked, true, `Unable to click ${selectorPrefix} card containing ${text}`)
}

async function setWorkerSwarmByAlias(page, alias, checked) {
  const result = await page.$$eval(
    `${testIdSelector('project-modal')} label`,
    (labels, needle, nextChecked) => {
      const label = labels.find((item) => (item.textContent || '').includes(String(needle)))
      if (!label) return false
      const input = label.querySelector('input[type="checkbox"]')
      if (!input) return false
      if (Boolean(input.checked) !== Boolean(nextChecked)) {
        input.click()
      }
      return true
    },
    alias,
    checked
  )
  assert.equal(result, true, `Worker swarm checkbox not found for alias ${alias}`)
}

async function setResumeWorkerSwarmByAlias(page, alias, checked) {
  const result = await page.$$eval(
    `${testIdSelector('project-resume-modal')} label`,
    (labels, needle, nextChecked) => {
      const label = labels.find((item) => (item.textContent || '').includes(String(needle)))
      if (!label) return false
      const input = label.querySelector('input[type="checkbox"]')
      if (!input) return false
      if (Boolean(input.checked) !== Boolean(nextChecked)) {
        input.click()
      }
      return true
    },
    alias,
    checked
  )
  assert.equal(result, true, `Resume worker swarm checkbox not found for alias ${alias}`)
}

async function launchMockSwarm(page, options) {
  const {
    alias,
    nodes = 1,
    prompt = 'You are a mock worker.',
    delayMs = 0,
    pushBranches = false
  } = options
  await clickTestId(page, 'open-launch-modal-button')
  await waitForTestId(page, 'launch-modal')
  await selectOptionByText(page, 'launch-provider-select', 'Local Mock Worker')
  await setValueByTestId(page, 'launch-alias-input', alias)
  await setValueByTestId(page, 'launch-nodes-input', String(nodes))
  await setValueByTestId(page, 'launch-system-prompt-input', prompt)
  await clickTestId(page, 'launch-provider-tab')
  await selectOptionByText(page, 'launch-provider-field-worker_mode', 'Mock')
  if (delayMs > 0) {
    await setValueByTestId(page, 'launch-provider-field-mock_delay_ms', String(delayMs))
  }
  if (pushBranches) {
    await setCheckboxByTestId(page, 'launch-provider-field-mock_push_branches', true)
  }
  await clickTestId(page, 'launch-submit-button')
  await waitForCardByText(page, 'swarm-card-', alias, 30_000)
}

async function waitForPromptResponse(page, promptText, timeoutMs = 30_000) {
  await page.waitForFunction(
    ([promptNeedle, responseNeedle]) => {
      const promptFound = Array.from(document.querySelectorAll('[data-testid="turn-prompt-bubble"]')).some((el) =>
        (el.textContent || '').includes(String(promptNeedle))
      )
      const responseFound = Array.from(document.querySelectorAll('[data-testid="turn-response-bubble"]')).some((el) =>
        (el.textContent || '').includes(String(responseNeedle))
      )
      return promptFound && responseFound
    },
    { timeout: timeoutMs },
    [promptText, 'Mock worker received prompt:']
  )
}

async function createDirectProject(page, options) {
  const { title, repoPath, workerAlias, tasksJson, autoStart = true } = options
  await clickTestId(page, 'open-project-modal-button')
  await waitForTestId(page, 'project-modal')
  await clickTestId(page, 'project-mode-tasks')
  await setValueByTestId(page, 'project-title-input', title)
  await setValueByTestId(page, 'project-repo-path-input', repoPath)
  await setWorkerSwarmByAlias(page, workerAlias, true)
  if (!autoStart) {
    await setCheckboxByTestId(page, 'project-auto-start-checkbox', false)
  }
  await setValueByTestId(page, 'project-tasks-json-input', tasksJson)
  await clickTestId(page, 'project-submit-button')
  await waitForCardByText(page, 'project-card-', title, 30_000)
}

async function createPlannedProject(page, options) {
  const { title, repoPath, plannerAlias, workerAlias, spec } = options
  await clickTestId(page, 'open-project-modal-button')
  await waitForTestId(page, 'project-modal')
  await clickTestId(page, 'project-mode-plan')
  await setValueByTestId(page, 'project-title-input', title)
  await setValueByTestId(page, 'project-repo-path-input', repoPath)
  await selectOptionByText(page, 'project-planner-swarm-select', plannerAlias)
  await setWorkerSwarmByAlias(page, workerAlias, true)
  await setValueByTestId(page, 'project-spec-input', spec)
  await clickTestId(page, 'project-submit-button')
}

async function createGitHubPlannedProject(page, options) {
  const {
    title,
    plannerAlias,
    workerAlias,
    githubOwner,
    githubRepo,
    baseBranch,
    spec
  } = options
  await clickTestId(page, 'open-project-modal-button')
  await waitForTestId(page, 'project-modal')
  await clickTestId(page, 'project-mode-plan')
  await clickTestId(page, 'project-repo-mode-github')
  await setValueByTestId(page, 'project-title-input', title)
  await setValueByTestId(page, 'project-base-branch-input', baseBranch)
  await setValueByTestId(page, 'project-github-owner-input', githubOwner)
  await setValueByTestId(page, 'project-github-repo-input', githubRepo)
  await setCheckboxByTestId(page, 'project-github-create-if-missing', false)
  await selectOptionByText(page, 'project-planner-swarm-select', plannerAlias)
  await setWorkerSwarmByAlias(page, workerAlias, true)
  await setValueByTestId(page, 'project-spec-input', spec)
  await clickTestId(page, 'project-submit-button')
  await waitForCardByText(page, 'project-card-', title, 30_000)
}

function createOrphanRemoteBranch(repoRef, branch) {
  const baseDir = fs.mkdtempSync(path.join(os.tmpdir(), 'codeswarm-ui-gh-'))
  try {
    const repoDir = path.join(baseDir, 'repo')
    runOrThrow(['git', 'clone', `git@github.com:${repoRef}.git`, repoDir], ROOT)
    runOrThrow(['git', '-C', repoDir, 'config', 'user.email', 'ui-smoke@example.test'], ROOT)
    runOrThrow(['git', '-C', repoDir, 'config', 'user.name', 'Codeswarm UI Smoke'], ROOT)
    runOrThrow(['git', '-C', repoDir, 'checkout', '--orphan', branch], ROOT)
    spawnSync('git', ['-C', repoDir, 'rm', '-rf', '.'], { encoding: 'utf8' })
    spawnSync('git', ['-C', repoDir, 'clean', '-fdx'], { encoding: 'utf8' })
    runOrThrow(['git', '-C', repoDir, 'commit', '--allow-empty', '-m', `empty base ${branch}`], ROOT)
    runOrThrow(['git', '-C', repoDir, 'push', '--set-upstream', 'origin', branch], ROOT)
  } finally {
    fs.rmSync(baseDir, { recursive: true, force: true })
  }
}

function deleteRemoteBranches(repoRef, branches) {
  const baseDir = fs.mkdtempSync(path.join(os.tmpdir(), 'codeswarm-ui-gh-clean-'))
  try {
    const repoDir = path.join(baseDir, 'repo')
    runOrThrow(['git', 'clone', `git@github.com:${repoRef}.git`, repoDir], ROOT)
    for (const branch of branches) {
      if (!branch) continue
      spawnSync('git', ['-C', repoDir, 'push', 'origin', `:${branch}`], { encoding: 'utf8' })
    }
  } finally {
    fs.rmSync(baseDir, { recursive: true, force: true })
  }
}

let stack
let browser
const tempRepoRoots = []

test('Codeswarm browser UI', { timeout: TEST_TIMEOUT_MS }, async (t) => {
  stack = await startStack()
  browser = await puppeteer.launch({
    headless: true,
    userDataDir: path.join(stack.tempRoot, 'chrome-profile'),
    env: {
      ...process.env,
      HOME: stack.tempRoot
    },
    args: [
      '--disable-breakpad',
      '--disable-crash-reporter',
      '--disable-crashpad',
      '--no-default-browser-check',
      '--no-first-run'
    ]
  })

  try {
    await t.test('renders project modal repo mode controls', async () => {
      const { context, page } = await newPage(browser, stack)
      try {
        await clickTestId(page, 'open-project-modal-button')
        await waitForTestId(page, 'project-modal')
        await waitForTestId(page, 'project-repo-path-input')
        await clickTestId(page, 'project-repo-mode-github')
        await waitForTestId(page, 'project-github-owner-input')
        await waitForTestId(page, 'project-github-repo-input')
        await waitForTestId(page, 'project-github-visibility-select')
      } finally {
        await context.close()
      }
    })

    await t.test('keeps websocket connected through initial UI activity', async () => {
      const { context, page } = await newPage(browser, stack)
      try {
        await waitForTestIdText(page, 'ws-status', /connected/i, 30_000)
        await clickTestId(page, 'open-launch-modal-button')
        await waitForTestId(page, 'launch-modal')
        await clickTestId(page, 'launch-cancel-button')
        await clickTestId(page, 'open-project-modal-button')
        await waitForTestId(page, 'project-modal')
        await clickTestId(page, 'project-cancel-button')
        await new Promise((resolve) => setTimeout(resolve, 2500))
        const wsStatusText = await page.$eval(testIdSelector('ws-status'), (el) => el.textContent || '')
        assert.match(wsStatusText, /connected/i)
        assert.doesNotMatch(wsStatusText, /disconnected/i)
      } finally {
        await context.close()
      }
    })

    await t.test('launches a mock swarm and shows prompt/response bubbles', async () => {
      const { context, page } = await newPage(browser, stack)
      const alias = `ui-chat-${Date.now()}`
      try {
        await launchMockSwarm(page, {
          alias,
          prompt: 'You are a browser UI smoke worker.',
          delayMs: 250
        })
        await clickCardByText(page, 'swarm-card-', alias)
        await waitForTestIdText(page, 'swarm-detail-title', new RegExp(alias.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
        await waitForTestIdText(page, 'swarm-detail-title', new RegExp(alias.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
        await page.waitForFunction(() => {
          return Array.from(document.querySelectorAll('div')).some((el) => {
            const text = el.textContent || ''
            return text.includes('Runtime: mock') && text.includes('Launch Profile: local-mock-worker')
          })
        }, { timeout: 30_000 })
        const promptText = 'Summarize your readiness for the UI smoke test.'
        await waitForTestId(page, 'swarm-prompt-input')
        await page.focus(testIdSelector('swarm-prompt-input'))
        await page.keyboard.type(promptText)
        await page.keyboard.press('Enter')
        await waitForPromptResponse(page, promptText, 30_000)
      } finally {
        await context.close()
      }
    })

    await t.test('applies orchestrated worker provider defaults in launch modal', async () => {
      const { context, page } = await newPage(browser, stack)
      try {
        await clickTestId(page, 'open-launch-modal-button')
        await waitForTestId(page, 'launch-modal')
        await selectOptionByText(page, 'launch-provider-select', 'Local Orchestrated Worker')
        await clickTestId(page, 'launch-provider-tab')
        assert.equal(await checkboxStateByTestId(page, 'launch-provider-field-native_auto_approve'), true)
        assert.equal(await checkboxStateByTestId(page, 'launch-provider-field-fresh_thread_per_injection'), true)
      } finally {
        await context.close()
      }
    })

    await t.test('shows Claude as an available local agent runtime', async () => {
      const { context, page } = await newPage(browser, stack)
      try {
        await clickTestId(page, 'open-launch-modal-button')
        await waitForTestId(page, 'launch-modal')
        await selectOptionByText(page, 'launch-provider-select', 'Local Dev')
        await clickTestId(page, 'launch-provider-tab')
        const runtimeOption = await optionStateByText(page, 'launch-provider-field-worker_mode', 'Claude')
        assert.equal(runtimeOption.disabled, false)
      } finally {
        await context.close()
      }
    })

    await t.test('shows the Claude gateway env profile in launch modal', async () => {
      const { context, page } = await newPage(browser, stack)
      try {
        await clickTestId(page, 'open-launch-modal-button')
        await waitForTestId(page, 'launch-modal')
        await selectOptionByText(page, 'launch-provider-select', 'Local Dev')
        await clickTestId(page, 'launch-provider-tab')
        const profileOption = await optionStateByText(
          page,
          'launch-provider-field-claude_env_profile',
          'amd-llm-gateway'
        )
        assert.equal(profileOption.disabled, false)
        const selectedValue = await page.$eval(
          testIdSelector('launch-provider-field-claude_env_profile'),
          (el) => el.value
        )
        assert.equal(selectedValue, 'amd-llm-gateway')
      } finally {
        await context.close()
      }
    })

    await t.test('disables failed providers in the launch modal', async () => {
      const { context, page } = await newPage(browser, stack)
      try {
        await page.evaluate(() => {
          const originalFetch = window.fetch.bind(window)
          window.fetch = async (input, init) => {
            const url = typeof input === 'string' ? input : input instanceof Request ? input.url : String(input)
            if (url.endsWith('/providers')) {
              return new Response(
                JSON.stringify([
                  {
                    id: 'local-disabled',
                    label: 'Local Disabled',
                    backend: 'local',
                    disabled: true,
                    disabled_reason: 'Router is disconnected',
                    defaults: {},
                    launch_fields: [],
                    launch_panels: []
                  },
                  {
                    id: 'aws-disabled',
                    label: 'AWS Disabled',
                    backend: 'aws',
                    disabled: true,
                    disabled_reason: 'Timed out during provider reconcile',
                    defaults: {},
                    launch_fields: [],
                    launch_panels: []
                  }
                ]),
                {
                  status: 200,
                  headers: { 'Content-Type': 'application/json' }
                }
              )
            }
            return originalFetch(input, init)
          }
        })

        await clickTestId(page, 'open-launch-modal-button')
        await waitForTestId(page, 'launch-modal')
        await waitForTestId(page, 'launch-provider-select')

        const localOption = await optionStateByText(page, 'launch-provider-select', 'Local Disabled')
        assert.equal(localOption.disabled, true)
        assert.match(localOption.text, /\(disabled\)/)

        const disabledOption = await optionStateByText(page, 'launch-provider-select', 'AWS Disabled')
        assert.equal(disabledOption.disabled, true)
        assert.match(disabledOption.text, /\(disabled\)/)

        const disabledLaunchDisabled = await page.$eval(
          testIdSelector('launch-submit-button'),
          (el) => Boolean(el.disabled)
        )
        assert.equal(disabledLaunchDisabled, true)
        await waitForTestIdText(page, 'launch-modal', /All configured providers are currently disabled/)
        await waitForTestIdText(page, 'launch-modal', /Router is disconnected/)
      } finally {
        await context.close()
      }
    })

    await t.test('creates a direct-task project and surfaces live worker activity', async () => {
      const repo = await createRepoWithOrigin('direct-ui')
      tempRepoRoots.push(repo.baseDir)
      const { context, page } = await newPage(browser, stack)
      const workerAlias = `ui-direct-worker-${Date.now()}`
      const title = `UI Direct Project ${Date.now()}`
      const tasks = JSON.stringify(
        [
          {
            task_id: 'T-001',
            title: 'Create alpha file',
            prompt: 'Create a file `ui-direct/alpha.txt` containing exactly `alpha direct`.',
            acceptance_criteria: ['`ui-direct/alpha.txt` exists with exact content `alpha direct`.'],
            depends_on: [],
            owned_paths: ['ui-direct/alpha.txt']
          },
          {
            task_id: 'T-002',
            title: 'Create bravo file',
            prompt: 'Create a file `ui-direct/bravo.txt` containing exactly `bravo direct`.',
            acceptance_criteria: ['`ui-direct/bravo.txt` exists with exact content `bravo direct`.'],
            depends_on: ['T-001'],
            owned_paths: ['ui-direct/bravo.txt']
          }
        ],
        null,
        2
      )
      try {
        await launchMockSwarm(page, {
          alias: workerAlias,
          prompt: '',
          delayMs: 1800,
          pushBranches: true
        })
        await waitForSwarmReady(stack, workerAlias, 60_000)
        await createDirectProject(page, {
          title,
          repoPath: repo.repoDir,
          workerAlias,
          tasksJson: tasks
        })
        const project = await waitForProject(stack, title, () => true, 60_000)
        assert.equal(project.repo_mode, 'local_path')
        assert.ok(project.tasks?.['T-001'], 'Expected direct project task T-001')
        await clickCardByText(page, 'project-card-', title)
        await waitForTestIdText(page, 'project-detail-title', new RegExp(title.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
        await waitForTestIdText(page, 'project-task-prompt', /Create a file `ui-direct\/alpha\.txt` containing exactly `alpha direct`\./)
        await waitForTestId(page, 'project-task-row-T-001')
      } finally {
        await context.close()
      }
    })

    await t.test('resumes a draft project from the project UI', async () => {
      const repo = await createRepoWithOrigin('resume-ui')
      tempRepoRoots.push(repo.baseDir)
      const { context, page } = await newPage(browser, stack)
      const workerAlias = `ui-resume-worker-${Date.now()}`
      const title = `UI Resume Project ${Date.now()}`
      const tasks = JSON.stringify(
        [
          {
            task_id: 'T-001',
            title: 'Create resume alpha file',
            prompt: 'Create a file `ui-resume/alpha.txt` containing exactly `alpha resume ui`.',
            acceptance_criteria: ['`ui-resume/alpha.txt` exists with exact content `alpha resume ui`.'],
            depends_on: [],
            owned_paths: ['ui-resume/alpha.txt']
          },
          {
            task_id: 'T-002',
            title: 'Create resume bravo file',
            prompt: 'Create a file `ui-resume/bravo.txt` containing exactly `bravo resume ui`.',
            acceptance_criteria: ['`ui-resume/bravo.txt` exists with exact content `bravo resume ui`.'],
            depends_on: ['T-001'],
            owned_paths: ['ui-resume/bravo.txt']
          }
        ],
        null,
        2
      )
      try {
        await launchMockSwarm(page, {
          alias: workerAlias,
          prompt: '',
          delayMs: 500,
          pushBranches: true
        })
        await waitForSwarmReady(stack, workerAlias, 60_000)
        await createDirectProject(page, {
          title,
          repoPath: repo.repoDir,
          workerAlias,
          tasksJson: tasks,
          autoStart: false
        })
        const draftProject = await waitForProject(stack, title, (item) => item.status === 'draft', 60_000)
        assert.equal(draftProject.status, 'draft')
        await clickCardByText(page, 'project-card-', title)
        await waitForTestIdText(page, 'project-detail-title', new RegExp(title.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
        await clickTestId(page, 'project-open-resume-button')
        await waitForTestId(page, 'project-resume-modal')
        await waitForTestId(page, 'project-resume-preview')
        await waitForTestIdText(page, 'project-resume-preview', /No task state changes|completed 0|kept 0/i, 30_000)
        await clickTestId(page, 'project-resume-submit-button')
        const completedProject = await waitForProject(stack, title, (item) => item.status === 'completed', 120_000)
        assert.equal(completedProject.status, 'completed')
        assert.ok((completedProject.resume_count || 0) >= 1, 'Expected resume_count after UI resume')
        await waitForTestId(page, 'project-resume-summary')
      } finally {
        await context.close()
      }
    })

    await t.test('shows blocked resume preview and can clear it by terminating the blocking swarm', async () => {
      const repo = await createRepoWithOrigin('resume-blocked-ui')
      tempRepoRoots.push(repo.baseDir)
      const { context, page } = await newPage(browser, stack)
      const workerAliasA = `ui-resume-blocker-${Date.now()}`
      const workerAliasB = `ui-resume-replacement-${Date.now()}`
      const title = `UI Resume Blocked Project ${Date.now()}`
      const tasks = JSON.stringify(
        [
          {
            task_id: 'T-001',
            title: 'Create blocking alpha file',
            prompt: 'Create a file `ui-resume-blocked/alpha.txt` containing exactly `alpha blocked ui`.',
            acceptance_criteria: ['`ui-resume-blocked/alpha.txt` exists with exact content `alpha blocked ui`.'],
            depends_on: [],
            owned_paths: ['ui-resume-blocked/alpha.txt']
          }
        ],
        null,
        2
      )
      try {
        await launchMockSwarm(page, {
          alias: workerAliasA,
          prompt: '',
          delayMs: 6000,
          pushBranches: true
        })
        const workerA = await waitForSwarmReady(stack, workerAliasA, 60_000)
        await launchMockSwarm(page, {
          alias: workerAliasB,
          prompt: '',
          delayMs: 500,
          pushBranches: true
        })
        await waitForSwarmReady(stack, workerAliasB, 60_000)
        await createDirectProject(page, {
          title,
          repoPath: repo.repoDir,
          workerAlias: workerAliasA,
          tasksJson: tasks,
          autoStart: true
        })
        await waitForProject(
          stack,
          title,
          (item) => item.status === 'running' && Number(item.task_counts?.assigned ?? 0) >= 1,
          60_000
        )
        await clickCardByText(page, 'project-card-', title)
        await waitForTestIdText(page, 'project-detail-title', new RegExp(title.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
        await clickTestId(page, 'project-open-resume-button')
        await waitForTestId(page, 'project-resume-modal')
        await setResumeWorkerSwarmByAlias(page, workerAliasB, true)
        await waitForTestId(page, 'project-resume-preview-blocked')
        await clickTestId(page, `project-resume-terminate-swarm-${workerA.swarm_id}`)
        await page.waitForFunction(
          () => !document.querySelector('[data-testid="project-resume-preview-blocked"]'),
          { timeout: 45_000 }
        )
        await page.waitForFunction(
          (selector) => {
            const button = document.querySelector(selector)
            return Boolean(button && !button.hasAttribute('disabled'))
          },
          { timeout: 15_000 },
          testIdSelector('project-resume-submit-button')
        )
      } finally {
        await context.close()
      }
    })

    await t.test('creates a planner project from spec and shows generated tasks', async () => {
      const repo = await createRepoWithOrigin('plan-ui')
      tempRepoRoots.push(repo.baseDir)
      const { context, page } = await newPage(browser, stack)
      const plannerAlias = `ui-planner-${Date.now()}`
      const workerAlias = `ui-plan-worker-${Date.now()}`
      const title = `UI Planned Project ${Date.now()}`
      const graph = {
        tasks: [
          {
            task_id: 'T-001',
            title: 'Create plan alpha file',
            prompt: 'Create a file `ui-plan/alpha.txt` containing exactly `alpha plan`.',
            acceptance_criteria: ['`ui-plan/alpha.txt` exists with exact content `alpha plan`.'],
            depends_on: [],
            owned_paths: ['ui-plan/alpha.txt']
          },
          {
            task_id: 'T-002',
            title: 'Create plan bravo file',
            prompt: 'Create a file `ui-plan/bravo.txt` containing exactly `bravo plan`.',
            acceptance_criteria: ['`ui-plan/bravo.txt` exists with exact content `bravo plan`.'],
            depends_on: ['T-001'],
            owned_paths: ['ui-plan/bravo.txt']
          }
        ]
      }
      const spec = `Create a deterministic UI planning graph.\nMOCK_TASK_GRAPH_JSON: ${JSON.stringify(graph)}`
      try {
        await launchMockSwarm(page, {
          alias: plannerAlias,
          prompt: '',
          delayMs: 250
        })
        await waitForSwarmReady(stack, plannerAlias, 60_000)
        await launchMockSwarm(page, {
          alias: workerAlias,
          prompt: '',
          delayMs: 1400,
          pushBranches: true
        })
        await waitForSwarmReady(stack, workerAlias, 60_000)
        await createPlannedProject(page, {
          title,
          repoPath: repo.repoDir,
          plannerAlias,
          workerAlias,
          spec
        })
        const project = await waitForProject(stack, title, () => true, 120_000)
        assert.equal(project.repo_mode, 'local_path')
        assert.ok(project.tasks?.['T-001'], 'Expected planned project task T-001')
        assert.ok(project.tasks?.['T-002'], 'Expected planned project task T-002')
        await waitForTestIdText(page, 'swarm-detail-title', new RegExp(plannerAlias.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
        await waitForCardByText(page, 'project-card-', title, 30_000)
        await clickCardByText(page, 'project-card-', title)
        await waitForTestIdText(page, 'project-detail-title', new RegExp(title.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
        await waitForTestId(page, 'project-task-row-T-001')
        await waitForTestId(page, 'project-task-row-T-002')
      } finally {
        await context.close()
      }
    })

    await t.test(
      'supports GitHub repo mode in the browser (optional)',
      { skip: !process.env.CODESWARM_UI_TEST_GITHUB_REPO },
      async () => {
        const repoRef = process.env.CODESWARM_UI_TEST_GITHUB_REPO
        const [githubOwner, githubRepo] = String(repoRef).split('/', 2)
        assert.ok(githubOwner && githubRepo, 'CODESWARM_UI_TEST_GITHUB_REPO must be owner/repo')
        const { context, page } = await newPage(browser, stack)
        const plannerAlias = `ui-gh-planner-${Date.now()}`
        const workerAlias = `ui-gh-worker-${Date.now()}`
        const title = `UI GitHub Project ${Date.now()}`
        const baseBranch = `codeswarm-ui-gh-${Date.now()}`
        const graph = {
          tasks: [
            {
              task_id: 'T-001',
              title: 'Create GitHub alpha file',
              prompt: 'Create a file `ui-github/alpha.txt` containing exactly `alpha github`.',
              acceptance_criteria: ['`ui-github/alpha.txt` exists with exact content `alpha github`.'],
              depends_on: [],
              owned_paths: ['ui-github/alpha.txt']
            },
            {
              task_id: 'T-002',
              title: 'Create GitHub bravo file',
              prompt: 'Create a file `ui-github/bravo.txt` containing exactly `bravo github`.',
              acceptance_criteria: ['`ui-github/bravo.txt` exists with exact content `bravo github`.'],
              depends_on: ['T-001'],
              owned_paths: ['ui-github/bravo.txt']
            }
          ]
        }
        const spec = `Create a deterministic GitHub UI planning graph.\nMOCK_TASK_GRAPH_JSON: ${JSON.stringify(graph)}`
        const cleanupBranches = [baseBranch]
        try {
          createOrphanRemoteBranch(repoRef, baseBranch)
          await launchMockSwarm(page, {
            alias: plannerAlias,
            prompt: 'You are a GitHub planner UI smoke worker.',
            delayMs: 250
          })
          await launchMockSwarm(page, {
            alias: workerAlias,
            prompt: 'You are a GitHub worker UI smoke worker.',
            delayMs: 1200,
            pushBranches: true
          })
          await createGitHubPlannedProject(page, {
            title,
            plannerAlias,
            workerAlias,
            githubOwner,
            githubRepo,
            baseBranch,
            spec
          })
          await clickCardByText(page, 'project-card-', title)
          const project = await waitForProject(stack, title, (item) => item.status === 'completed', 180_000)
          cleanupBranches.push(project.integration_branch)
          for (const task of Object.values(project.tasks || {})) {
            if (task && task.branch) cleanupBranches.push(task.branch)
          }
          assert.equal(project.repo_mode, 'github')
          const verifyDir = await mkdtemp('codeswarm-ui-gh-verify-')
          try {
            runOrThrow(['git', 'clone', `git@github.com:${repoRef}.git`, path.join(verifyDir, 'repo')], ROOT)
            const repoDir = path.join(verifyDir, 'repo')
            runOrThrow(['git', '-C', repoDir, 'fetch', 'origin', project.integration_branch], ROOT)
            runOrThrow(['git', '-C', repoDir, 'checkout', '-B', project.integration_branch, `origin/${project.integration_branch}`], ROOT)
            assert.equal(fs.readFileSync(path.join(repoDir, 'ui-github/alpha.txt'), 'utf8').trim(), 'alpha github')
            assert.equal(fs.readFileSync(path.join(repoDir, 'ui-github/bravo.txt'), 'utf8').trim(), 'bravo github')
          } finally {
            await fsp.rm(verifyDir, { recursive: true, force: true })
          }
        } finally {
          deleteRemoteBranches(repoRef, cleanupBranches)
          await context.close()
        }
      }
    )
  } catch (err) {
    err.message = `${err.message}\n\nRecent stack logs:\n${stack.dumpLogs()}`
    throw err
  } finally {
    if (browser) {
      await browser.close()
    }
    for (const repoRoot of tempRepoRoots) {
      await fsp.rm(repoRoot, { recursive: true, force: true })
    }
    if (stack) {
      await stack.stop()
    }
  }
})
