import { execFileSync, spawn } from 'node:child_process'

const args = process.argv.slice(2)
let graphStage = 'draft'
let graphScope = 'page'
if (args[0] === '--reviewed') {
  graphStage = 'reviewed'
  args.shift()
} else if (args[0] === '--typed') {
  graphStage = 'typed'
  args.shift()
} else if (args[0] === '--draft') {
  graphStage = 'draft'
  args.shift()
} else if (args[0] === '--global') {
  graphStage = 'reviewed'
  graphScope = 'global'
  args.shift()
}

let pageCode = 'OV-1'
let rest = args
if (args[0] && !args[0].startsWith('--')) {
  ;[pageCode, ...rest] = args
}

function parsePortArg(values) {
  for (let i = 0; i < values.length; i += 1) {
    if (values[i] === '--port' && values[i + 1]) return values[i + 1]
  }
  return null
}

function stopExistingListener(port) {
  if (!port) return
  try {
    const output = execFileSync(
      'bash',
      ['-lc', 'ss -ltnp'],
      { encoding: 'utf8' }
    )
      .split('\n')
      .filter((line) => line.includes(`:${port} `))
      .join('\n')
      .trim()
    if (!output) return
    const pidMatches = [...output.matchAll(/pid=(\d+)/g)]
    const pids = [...new Set(pidMatches.map((match) => Number(match[1])).filter(Boolean))]
    for (const pid of pids) {
      try {
        process.kill(pid, 'SIGTERM')
      } catch {}
    }
  } catch {}
}

stopExistingListener(parsePortArg(rest))

const child = spawn(
  'npx',
  ['vite', '--strictPort', ...rest],
  {
    stdio: 'inherit',
    shell: true,
    env: {
      ...process.env,
      VITE_PAGE_CODE: pageCode,
      VITE_GRAPH_STAGE: graphStage,
      VITE_GRAPH_SCOPE: graphScope,
    },
  }
)

child.on('exit', (code) => {
  process.exit(code ?? 0)
})
