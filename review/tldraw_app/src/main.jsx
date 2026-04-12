import React from 'react'
import ReactDOM from 'react-dom/client'
import { Tldraw, createShapeId } from '@tldraw/tldraw'
import '@tldraw/tldraw/tldraw.css'
import './styles.css'

const DEFAULT_PAGE = import.meta.env.VITE_PAGE_CODE || 'OV-1'
const DEFAULT_STAGE = import.meta.env.VITE_GRAPH_STAGE || 'draft'
const DEFAULT_SCOPE = import.meta.env.VITE_GRAPH_SCOPE || 'page'
const REPO_ROOT = __OMGS_REPO_ROOT__
const PAGE_SET = ['OV-1', 'OV-2', 'OV-3', 'OV-4', 'OV-5', 'OV-6', 'OV-7', 'OV-8', 'LCOC-1', 'LCOC-2', 'LCOC-3', 'LCOC-4', 'LCOC-5', 'LCOC-6', 'LCOC-7', 'LCOC-8', 'LCOC-9', 'LCOC-10', 'LCOC-11', 'LCOC-12', 'LCOC-13', 'LCOC-14']
const DEFAULT_NOTES = [
  'Background NCCN page is locked and semi-transparent.',
  'Shapes can be moved and text-edited with native tldraw behavior.',
  'Use tldraw’s native arrow tool to add edges; export will translate node bindings into directed graph edges.',
]
const BACKGROUND_ASSET_ID = 'asset:bg'
const BACKGROUND_SHAPE_ID = createShapeId('bg')
const DEFAULT_BACKGROUND_WIDTH = 1584
const DEFAULT_BACKGROUND_HEIGHT = 1224

function pageIdPrefix(pageCode) {
  return pageCode.replace(/[^A-Za-z0-9]/g, '')
}

function nextNodeId(pageCode, index) {
  return `${pageIdPrefix(pageCode)}_N${String(index).padStart(2, '0')}`
}

function setPageCodeInUrl(pageCode) {
  const params = new URLSearchParams(window.location.search)
  params.set('page', pageCode)
  window.history.replaceState({}, '', `${window.location.pathname}?${params.toString()}`)
}

function toFsUrl(absPath) {
  return `/@fs${absPath}`
}

async function loadGraph(pageCode, stage, scope) {
  const graphPath = scope === 'global'
    ? `${REPO_ROOT}/data/processed/ov_2025/reviewed_graph/ov_2025_global.reviewed_graph.json`
    : `${REPO_ROOT}/data/processed/ov_2025/pages/${pageCode}/page_graph.${stage}.json`
  const response = await fetch(toFsUrl(graphPath))
  if (!response.ok) {
    throw new Error(`Failed to load ${scope === 'global' ? 'global reviewed graph' : `${pageCode} ${stage} graph`} from ${graphPath}`)
  }
  return response.json()
}

function getNodeBBox(node) {
  const bbox = node.global_bbox || node.page_local_bbox || node.bbox
  if (Array.isArray(bbox) && bbox.length === 4) return bbox
  if (node.page_code === 'EXTERNAL') {
    const seed = String(node.id || node.local_node_id || 'EXT')
      .split('')
      .reduce((acc, ch) => acc + ch.charCodeAt(0), 0)
    const col = seed % 2
    const row = Math.floor(seed / 2) % 8
    return [24000 + (col * 320), 400 + (row * 180), 220, 72]
  }
  return [0, 0, 220, 72]
}

function fillForType(type) {
  if (type === 'reference' || type === 'cross_page') return 'solid'
  if (type === 'decision') return 'semi'
  if (type === 'stage') return 'pattern'
  return 'none'
}

function colorForType(type) {
  if (type === 'reference' || type === 'cross_page') return 'yellow'
  if (type === 'decision') return 'orange'
  if (type === 'stage') return 'blue'
  return 'black'
}

function colorForNodeLabel(nodeLabel, fallbackType) {
  if (nodeLabel === 'Disease Condition') return 'blue'
  if (nodeLabel === 'Treatment Option') return 'green'
  if (nodeLabel === 'Evaluation') return 'orange'
  if (nodeLabel === 'Page Jump') return 'yellow'
  return colorForType(fallbackType)
}

function nodeLabelForColor(color) {
  if (color === 'blue') return 'Disease Condition'
  if (color === 'green') return 'Treatment Option'
  if (color === 'orange') return 'Evaluation'
  if (color === 'yellow') return 'Page Jump'
  return null
}

function colorForNode(node, graphStage) {
  if (graphStage === 'typed' && node.node_label) {
    return colorForNodeLabel(node.node_label, node.node_type)
  }
  return colorForType(node.node_type)
}

function fillForNode(node, graphStage) {
  if (graphStage === 'typed') return 'none'
  return fillForType(node.node_type)
}

const NODE_CLASS_LEGEND = [
  { color: 'black', swatch: '#1d1d1d', token: 'black', label: 'untyped / null' },
  { color: 'blue', swatch: '#4465e9', token: 'blue', label: 'Disease Condition' },
  { color: 'green', swatch: '#099268', token: 'green', label: 'Treatment Option' },
  { color: 'orange', swatch: '#e16919', token: 'orange', label: 'Evaluation' },
  { color: 'yellow', swatch: '#f1ac4b', token: 'yellow', label: 'Page Jump' },
]

function getNodeCenter(node) {
  return {
    x: node.x + (node.w / 2),
    y: node.y + (node.h / 2),
  }
}

function createDefaultNodeShape(node, graphStage) {
  const [x, y, w, h] = getNodeBBox(node)
  const nodeType = node.node_type || 'process'
  const nodeLabel = typeof node.node_label === 'string' ? node.node_label : null
  const whyNode = typeof node.why_node === 'string' ? node.why_node : ''
  return {
    id: createShapeId(node.id),
    type: 'geo',
    x,
    y,
    meta: {
      nodeId: node.id,
      nodeType,
      nodeLabel,
      uncertain: node.is_uncertain,
      why: whyNode,
      kind: 'node',
    },
    props: {
      geo: 'rectangle',
      w,
      h,
      text: node.verbatim_text,
      color: colorForNode({ ...node, node_type: nodeType, node_label: nodeLabel }, graphStage),
      labelColor: 'black',
      fill: fillForNode({ ...node, node_type: nodeType }, graphStage),
      dash: 'draw',
      size: 's',
      font: 'serif',
      align: 'middle',
      verticalAlign: 'middle',
      url: '',
      growY: 0,
      scale: 1,
    },
  }
}

function createDefaultEdgeShape(edge, nodes) {
  const source = nodes.find((node) => node.id === edge.source_node_id)
  const target = nodes.find((node) => node.id === edge.target_node_id)
  if (!source || !target) return null
  const [sx, sy, sw, sh] = getNodeBBox(source)
  const [tx, ty, tw, th] = getNodeBBox(target)
  const start = getNodeCenter({ x: sx, y: sy, w: sw, h: sh })
  const end = getNodeCenter({ x: tx, y: ty, w: tw, h: th })
  return {
    id: createShapeId(edge.id),
    type: 'arrow',
    x: 0,
    y: 0,
    meta: {
      edgeId: edge.id,
      sourceNodeId: edge.source_node_id,
      targetNodeId: edge.target_node_id,
      uncertain: edge.is_uncertain,
      why: typeof edge.why_edge === 'string' ? edge.why_edge : '',
      kind: 'edge',
    },
    props: {
      color: 'black',
      labelColor: 'black',
      fill: 'none',
      dash: edge.is_uncertain ? 'dashed' : 'draw',
      size: 's',
      arrowheadStart: 'none',
      arrowheadEnd: 'arrow',
      font: 'serif',
      start: { x: start.x, y: start.y },
      end: { x: end.x, y: end.y },
      bend: 0,
      text: edge.edge_label || '',
      labelPosition: 0.5,
      scale: 1,
    },
  }
}

function createDefaultShapes(draft, graphStage) {
  return [
    ...draft.nodes.map((node) => createDefaultNodeShape(node, graphStage)),
    ...draft.edges.map((edge) => createDefaultEdgeShape(edge, draft.nodes)).filter(Boolean),
  ]
}

function createDefaultBindings(draft) {
  return draft.edges.flatMap((edge) => ([
    {
      type: 'arrow',
      fromId: createShapeId(edge.id),
      toId: createShapeId(edge.source_node_id),
      props: {
        terminal: 'start',
        normalizedAnchor: { x: 0.5, y: 0.5 },
        isExact: false,
        isPrecise: false,
      },
    },
    {
      type: 'arrow',
      fromId: createShapeId(edge.id),
      toId: createShapeId(edge.target_node_id),
      props: {
        terminal: 'end',
        normalizedAnchor: { x: 0.5, y: 0.5 },
        isExact: false,
        isPrecise: false,
      },
    },
  ]))
}

function ensureBackgroundShape(editor, draft, visible) {
  const existing = editor.getShape(BACKGROUND_SHAPE_ID)
  if (!visible) {
    if (existing) {
      editor.updateShapes([
        {
          id: BACKGROUND_SHAPE_ID,
          type: 'image',
          isLocked: false,
          opacity: 0,
          props: {
            ...existing.props,
          },
        },
      ])
      editor.deleteShapes([BACKGROUND_SHAPE_ID])
    }
    return
  }

  const backgroundImagePath = draft.background_image_path || `${REPO_ROOT}/data/raw/ov_2025/page_assets/page_${String(draft.page_number).padStart(3, '0')}.png`
  editor.createAssets([
    {
      id: BACKGROUND_ASSET_ID,
      type: 'image',
      typeName: 'asset',
      props: {
        name: backgroundImagePath.split('/').pop(),
        src: toFsUrl(backgroundImagePath),
        w: DEFAULT_BACKGROUND_WIDTH,
        h: DEFAULT_BACKGROUND_HEIGHT,
        mimeType: 'image/png',
        isAnimated: false,
      },
      meta: {},
    },
  ])

  if (existing) return

  editor.createShapes([
    {
      id: BACKGROUND_SHAPE_ID,
      type: 'image',
      x: 80,
      y: 80,
      isLocked: true,
      opacity: 0.45,
      props: {
        assetId: BACKGROUND_ASSET_ID,
        w: DEFAULT_BACKGROUND_WIDTH,
        h: DEFAULT_BACKGROUND_HEIGHT,
      },
    },
  ])
}

function getDirectedEdgeFromArrow(editor, shape, shapeIdToNodeId) {
  const bindings = editor.getBindingsFromShape(shape, 'arrow')
  const startBinding = bindings.find((binding) => binding.props.terminal === 'start')
  const endBinding = bindings.find((binding) => binding.props.terminal === 'end')
  return {
    id: shape.meta?.edgeId || shape.id,
    shape_id: shape.id,
    source_node_id: startBinding ? (shapeIdToNodeId.get(startBinding.toId) || null) : null,
    target_node_id: endBinding ? (shapeIdToNodeId.get(endBinding.toId) || null) : null,
    edge_type: 'flow',
    edge_label: shape.props.text || null,
    is_uncertain: Boolean(shape.meta?.uncertain),
    why_edge: shape.meta?.why || 'Exported from tldraw arrow shape.',
    start: shape.props.start,
    end: shape.props.end,
    bend: shape.props.bend,
  }
}

function pruneDanglingArrows(editor) {
  const pageShapes = editor.getCurrentPageShapes()
  const validNodeIds = new Set(
    pageShapes
      .filter((shape) => shape.type === 'geo' && shape.meta?.nodeId)
      .map((shape) => shape.id)
  )
  const danglingArrowIds = pageShapes
    .filter((shape) => shape.type === 'arrow')
    .filter((shape) => {
      const bindings = editor.getBindingsFromShape(shape, 'arrow')
      const startBinding = bindings.find((binding) => binding.props.terminal === 'start')
      const endBinding = bindings.find((binding) => binding.props.terminal === 'end')
      return !startBinding || !endBinding || !validNodeIds.has(startBinding.toId) || !validNodeIds.has(endBinding.toId)
    })
    .map((shape) => shape.id)

  if (danglingArrowIds.length) {
    editor.deleteShapes(danglingArrowIds)
  }
  return danglingArrowIds.length
}

function normalizeExportEdgeIds(pageCode, edgePayload) {
  const prefix = `${pageIdPrefix(pageCode)}_E`
  const used = new Set()
  return edgePayload.map((edge, index) => {
    let id = edge.id
    if (!id || used.has(id) || String(id).startsWith('shape:')) {
      id = `${prefix}${String(index + 1).padStart(2, '0')}`
      while (used.has(id)) {
        id = `${prefix}${String(used.size + index + 1).padStart(2, '0')}`
      }
    }
    used.add(id)
    return { ...edge, id }
  })
}

function collectNodePayload(pageCode, pageShapes) {
  const nodeShapes = pageShapes.filter((shape) => shape.type === 'geo')
  const used = new Set(
    nodeShapes
      .map((shape) => shape.meta?.nodeId)
      .filter(Boolean)
  )
  let nextIndex = 1
  const shapeIdToNodeId = new Map()

  const nodePayload = nodeShapes.map((shape) => {
    let nodeId = shape.meta?.nodeId || null
    if (!nodeId) {
      while (used.has(nextNodeId(pageCode, nextIndex))) {
        nextIndex += 1
      }
      nodeId = nextNodeId(pageCode, nextIndex)
      used.add(nodeId)
      nextIndex += 1
    }
    shapeIdToNodeId.set(shape.id, nodeId)
    return {
      id: nodeId,
      shape_id: shape.id,
      node_type: shape.meta?.nodeType || 'process',
      node_label: (() => {
        const explicit = typeof shape.meta?.nodeLabel === 'string' ? shape.meta.nodeLabel : null
        if (explicit) return explicit
        return nodeLabelForColor(shape.props?.color)
      })(),
      is_uncertain: Boolean(shape.meta?.uncertain),
      why_node: shape.meta?.why || '',
      verbatim_text: shape.props.text,
      text_snippet: shape.props.text.length > 88 ? `${shape.props.text.slice(0, 85)}...` : shape.props.text,
      bbox: [
        Math.round(shape.x),
        Math.round(shape.y),
        Math.round(shape.props.w),
        Math.round(shape.props.h),
      ],
    }
  })

  return { nodePayload, shapeIdToNodeId }
}

function normalizeCanvasNodes(editor, pageCode, graphStage) {
  const pageShapes = editor.getCurrentPageShapes()
  const nodeShapes = pageShapes.filter((shape) => shape.type === 'geo')
  const used = new Set(
    nodeShapes
      .map((shape) => shape.meta?.nodeId)
      .filter(Boolean)
  )
  let nextIndex = 1
  const updates = []

  for (const shape of nodeShapes) {
    let nodeId = shape.meta?.nodeId || null
    if (!nodeId) {
      while (used.has(nextNodeId(pageCode, nextIndex))) {
        nextIndex += 1
      }
      nodeId = nextNodeId(pageCode, nextIndex)
      used.add(nodeId)
      nextIndex += 1
    }

    const nodeType = shape.meta?.nodeType || 'process'
    const explicitNodeLabel = typeof shape.meta?.nodeLabel === 'string' ? shape.meta.nodeLabel : null
    const colorDerivedNodeLabel = graphStage === 'typed'
      ? nodeLabelForColor(shape.props?.color)
      : null
    const nodeLabel = colorDerivedNodeLabel || explicitNodeLabel || null
    const normalizedColor = graphStage === 'typed'
      ? (nodeLabel ? colorForNodeLabel(nodeLabel, nodeType) : 'black')
      : 'black'
    const normalizedFill = graphStage === 'typed'
      ? 'none'
      : 'none'

    const shouldUpdateMeta =
      shape.meta?.nodeId !== nodeId ||
      shape.meta?.kind !== 'node' ||
      !shape.meta?.nodeType

    const shouldUpdateStyle =
      shape.props?.font !== 'serif' ||
      shape.props?.color !== normalizedColor ||
      shape.props?.labelColor !== 'black' ||
      shape.props?.fill !== normalizedFill ||
      shape.props?.dash !== 'draw' ||
      shape.props?.size !== 's' ||
      shape.props?.align !== 'middle' ||
      shape.props?.verticalAlign !== 'middle'

    if (!shouldUpdateMeta && !shouldUpdateStyle) continue

    updates.push({
      id: shape.id,
      type: 'geo',
      meta: {
        ...shape.meta,
        nodeId,
        nodeType,
        nodeLabel: typeof nodeLabel === 'string' ? nodeLabel : null,
        uncertain: Boolean(shape.meta?.uncertain),
        why: shape.meta?.why || '',
        kind: 'node',
      },
      props: {
        ...shape.props,
        font: 'serif',
        color: normalizedColor,
        labelColor: 'black',
        fill: normalizedFill,
        dash: 'draw',
        size: 's',
        align: 'middle',
        verticalAlign: 'middle',
      },
    })
  }

  if (updates.length) {
    editor.updateShapes(updates)
  }
  return updates.length
}

function App() {
  const initialized = React.useRef(false)
  const editorRef = React.useRef(null)
  const [exportMessage, setExportMessage] = React.useState('')
  const [selectedPage, setSelectedPage] = React.useState(DEFAULT_PAGE)
  const [graphStage] = React.useState(DEFAULT_STAGE)
  const [graphScope] = React.useState(DEFAULT_SCOPE)
  const [draft, setDraft] = React.useState(null)
  const [loading, setLoading] = React.useState(true)
  const [loadError, setLoadError] = React.useState('')
  const [showBackground, setShowBackground] = React.useState(true)

  React.useEffect(() => {
    let cancelled = false
    setLoading(true)
    setLoadError('')
    loadGraph(selectedPage, graphStage, graphScope)
      .then((payload) => {
        if (cancelled) return
        setDraft(payload)
        if (graphScope === 'page') {
          setPageCodeInUrl(selectedPage)
        }
      })
      .catch((error) => {
        if (cancelled) return
        setDraft(null)
        setLoadError(error.message)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
      initialized.current = false
      editorRef.current = null
      setExportMessage('')
    }
  }, [selectedPage, graphStage, graphScope])

  React.useEffect(() => {
    if (!draft) return undefined
    const timer = window.setInterval(() => {
      if (!editorRef.current) return
      normalizeCanvasNodes(editorRef.current, draft.page_code, graphStage)
    }, 400)
    return () => window.clearInterval(timer)
  }, [draft, graphStage])

  React.useEffect(() => {
    const editor = editorRef.current
    if (!editor || !draft || graphScope !== 'page') return
    ensureBackgroundShape(editor, draft, showBackground)
  }, [draft, graphScope, showBackground])

  const handleMount = React.useCallback((editor) => {
    if (initialized.current || !draft) return
    initialized.current = true
    editorRef.current = editor
    const existingIds = editor.getCurrentPageShapes().map((shape) => shape.id)
    if (existingIds.length) {
      editor.deleteShapes(existingIds)
    }
    const shapes = createDefaultShapes(draft, graphStage)
    if (graphScope === 'page') {
      ensureBackgroundShape(editor, draft, showBackground)
    }

    editor.createShapes(shapes)
    editor.createBindings(createDefaultBindings(draft))

    editor.zoomToFit({ animation: { duration: 0 } })
  }, [draft, graphScope, graphStage, showBackground])

  const handleExport = React.useCallback(() => {
    const editor = editorRef.current
    if (!editor || !draft) return
    if (graphScope !== 'page') return
    normalizeCanvasNodes(editor, draft.page_code, graphStage)
    const prunedArrowCount = pruneDanglingArrows(editor)
    if (prunedArrowCount) {
      setExportMessage(
        prunedArrowCount === 1
          ? 'Removed 1 dangling arrow before export.'
          : `Removed ${prunedArrowCount} dangling arrows before export.`
      )
    }
    const pageShapes = editor.getCurrentPageShapes()
    const { nodePayload, shapeIdToNodeId } = collectNodePayload(draft.page_code, pageShapes)
    const nodeIds = nodePayload.map((node) => node.id)
    const duplicateNodeIds = nodeIds.filter((id, index) => nodeIds.indexOf(id) !== index)
    if (duplicateNodeIds.length) {
      const unique = [...new Set(duplicateNodeIds)]
      window.alert(
        `Cannot export review JSON.\n\n` +
        `Duplicate node ids are not allowed.\n\n` +
        unique.join('\n')
      )
      setExportMessage(
        unique.length === 1
          ? 'Export blocked: 1 duplicate node id.'
          : `Export blocked: ${unique.length} duplicate node ids.`
      )
      return
    }
    const emptyTextNodes = nodePayload.filter((node) => !node.verbatim_text || !node.verbatim_text.trim())
    if (emptyTextNodes.length) {
      window.alert(
        `Cannot export review JSON.\n\n` +
        `Each node must have non-empty text.\n\n` +
        emptyTextNodes.map((node) => node.id).join('\n')
      )
      setExportMessage(
        emptyTextNodes.length === 1
          ? 'Export blocked: 1 node has empty text.'
          : `Export blocked: ${emptyTextNodes.length} nodes have empty text.`
      )
      return
    }
    const invalidTypeNodes = nodePayload.filter((node) => !['process', 'stage', 'decision', 'reference', 'cross_page'].includes(node.node_type))
    if (invalidTypeNodes.length) {
      window.alert(
        `Cannot export review JSON.\n\n` +
        `Each node must have a valid node_type.\n\n` +
        invalidTypeNodes.map((node) => `${node.id}: ${node.node_type}`).join('\n')
      )
      setExportMessage(
        invalidTypeNodes.length === 1
          ? 'Export blocked: 1 node has an invalid node_type.'
          : `Export blocked: ${invalidTypeNodes.length} nodes have invalid node_type values.`
      )
      return
    }
    const invalidBboxNodes = nodePayload.filter((node) => node.bbox[2] <= 0 || node.bbox[3] <= 0)
    if (invalidBboxNodes.length) {
      window.alert(
        `Cannot export review JSON.\n\n` +
        `Each node must have a valid bbox with positive width and height.\n\n` +
        invalidBboxNodes.map((node) => `${node.id}: [${node.bbox.join(', ')}]`).join('\n')
      )
      setExportMessage(
        invalidBboxNodes.length === 1
          ? 'Export blocked: 1 node has an invalid bbox.'
          : `Export blocked: ${invalidBboxNodes.length} nodes have invalid bbox values.`
      )
      return
    }
    let edgePayload = pageShapes
      .filter((shape) => shape.type === 'arrow')
      .map((shape) => getDirectedEdgeFromArrow(editor, shape, shapeIdToNodeId))
    edgePayload = normalizeExportEdgeIds(draft.page_code, edgePayload)
    const edgeIds = edgePayload.map((edge) => edge.id)
    const duplicateEdgeIds = edgeIds.filter((id, index) => edgeIds.indexOf(id) !== index)
    if (duplicateEdgeIds.length) {
      const unique = [...new Set(duplicateEdgeIds)]
      window.alert(
        `Cannot export review JSON.\n\n` +
        `Duplicate edge ids are not allowed.\n\n` +
        unique.join('\n')
      )
      setExportMessage(
        unique.length === 1
          ? 'Export blocked: 1 duplicate edge id.'
          : `Export blocked: ${unique.length} duplicate edge ids.`
      )
      return
    }
    const unresolvedEdges = edgePayload.filter((edge) => !edge.source_node_id || !edge.target_node_id)
    if (unresolvedEdges.length) {
      const lines = unresolvedEdges.map((edge) => {
        const missing = [
          edge.source_node_id ? null : 'source',
          edge.target_node_id ? null : 'target',
        ].filter(Boolean).join(' + ')
        return `${edge.id}: missing ${missing} binding`
      })
      window.alert(
        `Cannot export review JSON.\n\n` +
        `There ${unresolvedEdges.length === 1 ? 'is 1 unresolved arrow' : `are ${unresolvedEdges.length} unresolved arrows`}.\n` +
        `Each exported edge must be bound to both a source node and a target node.\n\n` +
        lines.join('\n')
      )
      setExportMessage(
        unresolvedEdges.length === 1
          ? 'Export blocked: 1 arrow is missing a node binding.'
          : `Export blocked: ${unresolvedEdges.length} arrows are missing node bindings.`
      )
      return
    }
    const missingNodeEdges = edgePayload.filter(
      (edge) => !nodeIds.includes(edge.source_node_id) || !nodeIds.includes(edge.target_node_id)
    )
    if (missingNodeEdges.length) {
      window.alert(
        `Cannot export review JSON.\n\n` +
        `Each edge must reference existing node ids.\n\n` +
        missingNodeEdges.map((edge) => `${edge.id}: ${edge.source_node_id} -> ${edge.target_node_id}`).join('\n')
      )
      setExportMessage(
        missingNodeEdges.length === 1
          ? 'Export blocked: 1 edge references a missing node.'
          : `Export blocked: ${missingNodeEdges.length} edges reference missing nodes.`
      )
      return
    }
    const duplicateGroups = new Map()
    edgePayload.forEach((edge) => {
      const key = `${edge.source_node_id}__${edge.target_node_id}`
      const group = duplicateGroups.get(key) || []
      group.push(edge)
      duplicateGroups.set(key, group)
    })
    const duplicatedEdges = [...duplicateGroups.values()].filter((group) => group.length > 1)
    if (duplicatedEdges.length) {
      const lines = duplicatedEdges.map((group) => {
        const sample = group[0]
        const ids = group.map((edge) => edge.id).join(', ')
        return `${sample.source_node_id} -> ${sample.target_node_id}: ${ids}`
      })
      window.alert(
        `Cannot export review JSON.\n\n` +
        `There ${duplicatedEdges.length === 1 ? 'is 1 duplicated directed edge' : `are ${duplicatedEdges.length} duplicated directed edges`}.\n` +
        `Each source -> target pair may appear only once.\n\n` +
        lines.join('\n')
      )
      setExportMessage(
        duplicatedEdges.length === 1
          ? 'Export blocked: 1 duplicated directed edge.'
          : `Export blocked: ${duplicatedEdges.length} duplicated directed edges.`
      )
      return
    }
    const payload = {
      page_code: draft.page_code,
      page_number: draft.page_number,
      graph_type: 'directed',
      nodes: nodePayload,
      edges: edgePayload,
    }
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = 'page_graph.reviewed.json'
    link.click()
    URL.revokeObjectURL(url)
    setExportMessage(`Exported ${nodePayload.length} nodes and ${edgePayload.length} edges.`)
  }, [draft, graphScope, graphStage])

  const handleDeleteSelected = React.useCallback(() => {
    const editor = editorRef.current
    if (!editor) return
    if (graphScope !== 'page') return
    const ids = editor.getSelectedShapeIds().filter((id) => {
      const shape = editor.getShape(id)
      return shape && shape.type !== 'image'
    })
    if (!ids.length) {
      setExportMessage('No selected nodes or edges to delete.')
      return
    }
    editor.deleteShapes(ids)
    setExportMessage(`Deleted ${ids.length} selected shape${ids.length === 1 ? '' : 's'}.`)
  }, [graphScope])

  React.useEffect(() => {
    const onKeyDown = (event) => {
      if (event.key !== 'Delete' && event.key !== 'Backspace') return
      const target = event.target
      const tagName = target?.tagName?.toLowerCase?.() || ''
      const isEditable =
        tagName === 'input' ||
        tagName === 'textarea' ||
        target?.isContentEditable
      if (isEditable) return
      if (!editorRef.current) return
      const selectedIds = editorRef.current.getSelectedShapeIds().filter((id) => {
        const shape = editorRef.current.getShape(id)
        return shape && shape.type !== 'image'
      })
      if (!selectedIds.length) return
      event.preventDefault()
      handleDeleteSelected()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [handleDeleteSelected])

  const handleRestore = React.useCallback(() => {
    const editor = editorRef.current
    if (!editor || !draft) return
    const ids = editor.getCurrentPageShapes().filter((shape) => shape.type !== 'image').map((shape) => shape.id)
    if (ids.length) editor.deleteShapes(ids)
    editor.createShapes(createDefaultShapes(draft, graphStage))
    editor.createBindings(createDefaultBindings(draft))
    editor.zoomToFit({ animation: { duration: 0 } })
    setExportMessage(`Restored the default ${draft.page_code} node and edge layout.`)
  }, [draft, graphStage])

  if (loading) {
    return (
      <div className="app-shell">
        <div className="app-header">
          <div className="header-copy">
            <strong>tldraw probe</strong>
            <span> loading {selectedPage} {graphStage}...</span>
          </div>
        </div>
      </div>
    )
  }

  if (loadError || !draft) {
    return (
      <div className="app-shell">
        <div className="app-header">
          <div className="header-copy">
            <strong>tldraw probe</strong>
            <span> failed to load graph</span>
          </div>
        </div>
        <div className="app-main">
          <aside className="side-panel" style={{ maxWidth: 720 }}>
            <section className="panel-card">
              <h2>Load Error</h2>
              <p>{loadError || 'Unknown load error.'}</p>
              <p>Expected page draft path:</p>
              <p>{graphScope === 'global'
                ? `${REPO_ROOT}/data/processed/ov_2025/reviewed_graph/ov_2025_global.reviewed_graph.json`
                : `${REPO_ROOT}/data/processed/ov_2025/pages/${selectedPage}/page_graph.${graphStage}.json`}</p>
            </section>
          </aside>
        </div>
      </div>
    )
  }

  return (
    <div className="app-shell">
      <div className="app-header">
        <div className="header-copy">
          <strong>tldraw probe</strong>
          <span> {graphScope === 'global' ? 'OV 2025 global reviewed graph' : `${draft.page_code} ${graphStage} canvas with NCCN background and graph nodes`}</span>
        </div>
        <div className="header-actions">
          {graphScope === 'page' ? (
            <select value={selectedPage} onChange={(event) => setSelectedPage(event.target.value)}>
              {PAGE_SET.map((pageCode) => (
                <option key={pageCode} value={pageCode}>{pageCode}</option>
              ))}
            </select>
          ) : null}
            {graphScope === 'page' ? <button onClick={() => setShowBackground((value) => !value)}>{showBackground ? 'Hide Background' : 'Show Background'}</button> : null}
            <button onClick={handleRestore}>Restore Layout</button>
            {graphScope === 'page' ? <button onClick={handleDeleteSelected}>Delete Selected</button> : null}
            {graphScope === 'page' ? <button className="primary" onClick={handleExport}>Export Review JSON</button> : null}
        </div>
      </div>
      <div className="app-main">
        <div className="canvas-wrap">
          <Tldraw
            key={draft.page_code}
            inferDarkMode={false}
            onMount={handleMount}
          />
        </div>
        <aside className="side-panel">
          <section className="panel-card">
            <h2>Review Notes</h2>
            {[...(graphScope === 'global'
              ? [
                'This view loads the stitched OV 2025 global reviewed graph.',
                'There is no locked NCCN page background in global mode.',
                'Use this mode for graph browsing, not page-level review export.',
              ]
              : DEFAULT_NOTES), ...(draft.notes || [])].map((note) => <p key={note}>{note}</p>)}
          </section>
          <section className="panel-card">
            <h2>Current Scope</h2>
            <p>{draft.nodes.length} nodes loaded for {graphScope === 'global' ? 'OV 2025 global graph' : draft.page_code}.</p>
            <p>{draft.edges.length} edges loaded from the current {graphScope === 'global' ? 'global reviewed graph' : `${graphStage} graph`}.</p>
            <p>Graph stage: {graphStage}</p>
            {graphStage === 'typed' ? <p>Typed mode colors node borders by `node_label`: Disease Condition = blue, Treatment Option = green, Evaluation = orange, Page Jump = yellow. Black means null/untyped.</p> : null}
            <p>{graphScope === 'global' ? 'Global mode is view-oriented and does not export reviewed page JSON.' : 'Background page is reference-only; edit the overlay shapes.'}</p>
            <p>{graphScope === 'global' ? 'Use Restore Layout to reset the stitched global graph view.' : (exportMessage || 'Use Export Review JSON to save node and edge state together.')}</p>
          </section>
          <section className="panel-card">
            <h2>Node Class Legend</h2>
            <p>Export uses `shape.meta.nodeLabel` first. If missing, it falls back to the node border color mapping below.</p>
            <p>Only black / blue / green / orange / yellow are recognized for `node_label` recovery. Any other palette color falls back to `null`.</p>
            <div style={{ display: 'grid', gap: 8 }}>
              {NODE_CLASS_LEGEND.map((item) => (
                <div key={item.label} style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  <span
                    style={{
                      width: 12,
                      height: 12,
                      borderRadius: 999,
                      background: item.swatch,
                      display: 'inline-block',
                      border: item.color === 'yellow' ? '1px solid #d08f34' : '1px solid transparent',
                    }}
                  />
                  <span><strong>{item.token}</strong> {'->'} {item.label}</span>
                </div>
              ))}
            </div>
          </section>
        </aside>
      </div>
    </div>
  )
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)
