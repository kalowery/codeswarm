import { execSync } from 'child_process'

function checkNode() {
  const version = process.version
  console.log(`✅ Node ${version}`)
}

function checkPython() {
  try {
    const output = execSync('python3 --version', { encoding: 'utf-8' }).trim()
    const match = output.match(/Python (\d+)\.(\d+)/)
    if (!match || !match[1] || !match[2]) {
      throw new Error('Unknown Python version')
    }
    const major = parseInt(match[1], 10)
    const minor = parseInt(match[2], 10)

    if (major < 3 || (major === 3 && minor < 10)) {
      console.error(`❌ ${output} detected — Python 3.10+ required`)
      process.exit(1)
    }

    console.log(`✅ ${output}`)
  } catch (err) {
    console.error('❌ python3 not found or failed to execute')
    process.exit(1)
  }
}

function checkCodex() {
  try {
    execSync('codex login status', { stdio: 'ignore' })
    console.log('✅ Codex authenticated')
  } catch {
    console.error('❌ Codex not authenticated (run: codex login)')
    process.exit(1)
  }
}

function checkPort(port: number) {
  try {
    execSync(`lsof -i :${port}`, { stdio: 'ignore' })
    console.warn(`⚠️  Port ${port} is in use`)
  } catch {
    console.log(`✅ Port ${port} available`)
  }
}

export function runDoctor() {
  console.log('\nCodeswarm Doctor\n')
  checkNode()
  checkPython()
  checkCodex()
  checkPort(4000)
  console.log('\n✅ All critical checks passed.\n')
}
