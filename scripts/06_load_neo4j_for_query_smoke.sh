#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

CONTAINER_NAME="${OMGS_NCCN_NEO4J_CONTAINER:-omgs-nccn-neo4j}"
PASSWORD="${OMGS_NCCN_NEO4J_PASSWORD:-omgs-nccn-dev}"
IMPORT_DIR="${OMGS_NCCN_NEO4J_IMPORT_DIR:-/import}"
NODES_CSV="${OMGS_NCCN_NEO4J_NODES_CSV:-data/processed/ov_2025/query/ov_2025_global.neo4j_nodes.csv}"
EDGES_CSV="${OMGS_NCCN_NEO4J_EDGES_CSV:-data/processed/ov_2025/query/ov_2025_global.neo4j_edges.csv}"
NODES_CSV_BASENAME="$(basename "$NODES_CSV")"
EDGES_CSV_BASENAME="$(basename "$EDGES_CSV")"

echo "loading Neo4j query-smoke graph into container: ${CONTAINER_NAME}"
echo "nodes_csv_source=${NODES_CSV}"
echo "edges_csv_source=${EDGES_CSV}"
echo "neo4j_import_dir=${IMPORT_DIR}"

if [[ ! -f "$NODES_CSV" ]]; then
  echo "missing nodes CSV: $NODES_CSV"
  exit 1
fi

if [[ ! -f "$EDGES_CSV" ]]; then
  echo "missing edges CSV: $EDGES_CSV"
  exit 1
fi

if docker ps >/dev/null 2>&1; then
  DOCKER_CMD=(docker)
elif sudo -n docker ps >/dev/null 2>&1; then
  DOCKER_CMD=(sudo -n docker)
else
  echo "docker daemon is not accessible in this shell."
  echo "Either run this script from a shell with docker-group access, or run it manually and enter sudo when prompted."
  exit 1
fi

echo "copying query-smoke CSV exports into container import dir"
"${DOCKER_CMD[@]}" cp "${NODES_CSV}" "${CONTAINER_NAME}:${IMPORT_DIR}/${NODES_CSV_BASENAME}"
"${DOCKER_CMD[@]}" cp "${EDGES_CSV}" "${CONTAINER_NAME}:${IMPORT_DIR}/${EDGES_CSV_BASENAME}"

echo "clearing existing GuidelineNode smoke-test graph"
"${DOCKER_CMD[@]}" exec "${CONTAINER_NAME}" cypher-shell -u neo4j -p "${PASSWORD}" "
MATCH (n:GuidelineNode)
DETACH DELETE n;
"

"${DOCKER_CMD[@]}" exec "${CONTAINER_NAME}" cypher-shell -u neo4j -p "${PASSWORD}" "
CREATE CONSTRAINT guideline_node_id IF NOT EXISTS
FOR (n:GuidelineNode)
REQUIRE n.id IS UNIQUE;
"

"${DOCKER_CMD[@]}" exec "${CONTAINER_NAME}" cypher-shell -u neo4j -p "${PASSWORD}" "
LOAD CSV WITH HEADERS FROM 'file:///${NODES_CSV_BASENAME}' AS row
MERGE (n:GuidelineNode {id: row.\`node_id:ID(GuidelineNode)\`})
SET n.page_code = row.page_code,
    n.page_number = toInteger(row.\`page_number:int\`),
    n.node_type = row.node_type,
    n.node_label = row.node_label,
    n.verbatim_text = row.verbatim_text,
    n.text_snippet = row.text_snippet,
    n.guideline_header = row.guideline_header,
    n.page_title = row.page_title,
    n.page_scope_summary = row.page_scope_summary,
    n.explicit_ref_ids = CASE WHEN row.explicit_ref_ids = '' THEN [] ELSE split(row.explicit_ref_ids, '|') END,
    n.explicit_ref_count = toInteger(row.\`explicit_ref_count:int\`),
    n.has_explicit_refs = (row.\`has_explicit_refs:boolean\` = 'true'),
    n.reviewed_footnote_ids = CASE WHEN row.reviewed_footnote_ids = '' THEN [] ELSE split(row.reviewed_footnote_ids, '|') END,
    n.reviewed_footnote_labels = CASE WHEN row.reviewed_footnote_labels = '' THEN [] ELSE split(row.reviewed_footnote_labels, '|') END,
    n.reviewed_footnote_texts = CASE WHEN row.reviewed_footnote_texts = '' THEN [] ELSE split(row.reviewed_footnote_texts, '|') END,
    n.reviewed_footnote_count = toInteger(row.\`reviewed_footnote_count:int\`),
    n.has_reviewed_footnotes = (row.\`has_reviewed_footnotes:boolean\` = 'true'),
    n.reviewed_footnote_ref_ids = CASE WHEN row.reviewed_footnote_ref_ids = '' THEN [] ELSE split(row.reviewed_footnote_ref_ids, '|') END,
    n.reviewed_footnote_ref_count = toInteger(row.\`reviewed_footnote_ref_count:int\`),
    n.has_reviewed_footnote_refs = (row.\`has_reviewed_footnote_refs:boolean\` = 'true'),
    n.is_uncertain = (row.\`is_uncertain:boolean\` = 'true');
"

"${DOCKER_CMD[@]}" exec "${CONTAINER_NAME}" cypher-shell -u neo4j -p "${PASSWORD}" "
LOAD CSV WITH HEADERS FROM 'file:///${EDGES_CSV_BASENAME}' AS row
WITH row
WHERE row.\`:TYPE\` = 'REQUIRES'
MATCH (src:GuidelineNode {id: row.\`:START_ID(GuidelineNode)\`})
MATCH (dst:GuidelineNode {id: row.\`:END_ID(GuidelineNode)\`})
MERGE (src)-[r:REQUIRES {id: row.edge_id}]->(dst)
SET r.source_page_code = row.source_page_code,
    r.target_page_code = row.target_page_code,
    r.stitch_kind = row.stitch_kind,
    r.edge_type = row.edge_type,
    r.edge_label_text = row.edge_label_text,
    r.is_uncertain = (row.\`is_uncertain:boolean\` = 'true');
"

"${DOCKER_CMD[@]}" exec "${CONTAINER_NAME}" cypher-shell -u neo4j -p "${PASSWORD}" "
LOAD CSV WITH HEADERS FROM 'file:///${EDGES_CSV_BASENAME}' AS row
WITH row
WHERE row.\`:TYPE\` = 'INDICATES'
MATCH (src:GuidelineNode {id: row.\`:START_ID(GuidelineNode)\`})
MATCH (dst:GuidelineNode {id: row.\`:END_ID(GuidelineNode)\`})
MERGE (src)-[r:INDICATES {id: row.edge_id}]->(dst)
SET r.source_page_code = row.source_page_code,
    r.target_page_code = row.target_page_code,
    r.stitch_kind = row.stitch_kind,
    r.edge_type = row.edge_type,
    r.edge_label_text = row.edge_label_text,
    r.is_uncertain = (row.\`is_uncertain:boolean\` = 'true');
"

"${DOCKER_CMD[@]}" exec "${CONTAINER_NAME}" cypher-shell -u neo4j -p "${PASSWORD}" "
LOAD CSV WITH HEADERS FROM 'file:///${EDGES_CSV_BASENAME}' AS row
WITH row
WHERE row.\`:TYPE\` = 'IS_FOLLOWED_BY'
MATCH (src:GuidelineNode {id: row.\`:START_ID(GuidelineNode)\`})
MATCH (dst:GuidelineNode {id: row.\`:END_ID(GuidelineNode)\`})
MERGE (src)-[r:IS_FOLLOWED_BY {id: row.edge_id}]->(dst)
SET r.source_page_code = row.source_page_code,
    r.target_page_code = row.target_page_code,
    r.stitch_kind = row.stitch_kind,
    r.edge_type = row.edge_type,
    r.edge_label_text = row.edge_label_text,
    r.is_uncertain = (row.\`is_uncertain:boolean\` = 'true');
"

echo
echo "sanity checks:"
"${DOCKER_CMD[@]}" exec "${CONTAINER_NAME}" cypher-shell -u neo4j -p "${PASSWORD}" \
  "MATCH (n:GuidelineNode) RETURN count(n) AS node_count;"
"${DOCKER_CMD[@]}" exec "${CONTAINER_NAME}" cypher-shell -u neo4j -p "${PASSWORD}" \
  "MATCH ()-[r]->() RETURN type(r) AS relation_type, count(r) AS relation_count ORDER BY relation_type;"
