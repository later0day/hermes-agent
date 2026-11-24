import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'
import { describe, expect, it } from 'vitest'

function loadSwarmComponents(expanded) {
  const filename = fileURLToPath(new URL('../plugins/kanban/dashboard/dist/index.js', import.meta.url))
  const source = readFileSync(filename, 'utf8')
  const createElement = (type, props, ...children) => ({
    type,
    props: { ...(props || {}), children: children.length === 1 ? children[0] : children },
  })
  const window = {
    __HERMES_PLUGINS__: { register() {} },
    __HERMES_PLUGIN_SDK__: {
      React: { createElement, Fragment: Symbol('Fragment'), Component: class {} },
      components: {},
      hooks: {
        useState() {
          return [expanded.value, update => {
            expanded.value = typeof update === 'function' ? update(expanded.value) : update
          }]
        },
        useEffect() {},
        useCallback: fn => fn,
        useMemo: fn => fn(),
        useRef: current => ({ current }),
      },
      utils: {
        cn: (...names) => names.filter(Boolean).join(' '),
        timeAgo: String,
      },
    },
  }
  const end = source.lastIndexOf('})();')
  if (end < 0) throw new Error('Kanban plugin bundle did not evaluate')
  const instrumented = source.slice(0, end)
    + 'window.__swarmTest = { collectSwarmGraphs, SwarmGraphStrip };\n'
    + source.slice(end)
  vm.runInNewContext(instrumented, { window, console, URLSearchParams }, { filename })
  return window.__swarmTest
}

function render(node) {
  if (node == null || typeof node !== 'object') return node
  if (Array.isArray(node)) return node.map(render)
  if (typeof node.type === 'function') return render(node.type(node.props))
  return { ...node, props: { ...node.props, children: render(node.props.children) } }
}

function findAll(node, predicate, matches = []) {
  if (node == null || typeof node !== 'object') return matches
  if (Array.isArray(node)) {
    for (const child of node) findAll(child, predicate, matches)
    return matches
  }
  if (predicate(node)) matches.push(node)
  findAll(node.props?.children, predicate, matches)
  return matches
}

function textContent(node) {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textContent).join('')
  return textContent(node.props?.children)
}

function graph(number) {
  return {
    root_id: `root-${number}`,
    goal: `Goal ${number}`,
    status: 'running',
    counts: { total: 3, done: 1 },
    nodes: [
      { id: `root-${number}`, role: 'root', task: { title: 'Root', status: 'done' } },
      { id: `worker-${number}`, role: 'worker', task: { title: 'Worker', status: 'running', assignee: 'coder' } },
      { id: `verifier-${number}`, role: 'verifier', task: { title: 'Verifier', status: 'todo' } },
    ],
  }
}

describe('Kanban dashboard swarm graphs', () => {
  it('collects only the board payload swarm graph array', () => {
    const { collectSwarmGraphs } = loadSwarmComponents({ value: false })
    const graphs = [graph(1), graph(2)]

    expect(collectSwarmGraphs({ swarm_graphs: graphs })).toBe(graphs)
    expect(collectSwarmGraphs({ swarm_graphs: null })).toEqual([])
    expect(collectSwarmGraphs({})).toEqual([])
    expect(collectSwarmGraphs(null)).toEqual([])
  })

  it('collapses multiple graphs, expands them, and opens a node in the task drawer', () => {
    const expanded = { value: false }
    const { SwarmGraphStrip } = loadSwarmComponents(expanded)
    const opened = []
    const props = {
      boardData: { swarm_graphs: [graph(1), graph(2), graph(3)] },
      onOpen: id => opened.push(id),
    }
    const cards = tree => findAll(tree, node =>
      node.props?.className?.split(' ').includes('hermes-kanban-swarm-card'))

    const collapsed = render(SwarmGraphStrip(props))
    expect(cards(collapsed)).toHaveLength(2)
    expect(textContent(collapsed)).toContain('Swarm graphs (3)')
    expect(textContent(collapsed)).not.toContain('Goal 3')

    const toggle = findAll(collapsed, node =>
      node.props?.className === 'hermes-kanban-swarms-toggle')[0]
    toggle.props.onClick()
    const fullTree = render(SwarmGraphStrip(props))
    expect(cards(fullTree)).toHaveLength(3)
    expect(textContent(fullTree)).toContain('Goal 3')
    expect(textContent(fullTree)).toContain('1/3 done')

    const runningNode = findAll(fullTree, node =>
      node.props?.className?.includes('hermes-kanban-swarm-node--running'))[0]
    runningNode.props.onClick()
    expect(opened).toEqual(['worker-1'])
  })
})
