#!/usr/bin/env python3
"""V9 엔진 의존성 분석 및 React Flow HTML 생성."""

import os
import re
from pathlib import Path
from collections import defaultdict

def extract_imports(file_path):
    """Python 파일에서 임포트 추출."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        imports = []
        # from X import Y
        for match in re.finditer(r'from\s+([\w.]+)\s+import', content):
            imports.append(match.group(1))
        # import X
        for match in re.finditer(r'^import\s+([\w.]+)', content, re.MULTILINE):
            imports.append(match.group(1))

        return imports
    except:
        return []

def analyze_v9_structure():
    """V9 엔진 구조 분석."""
    v9_path = Path('/Users/codename/Downloads/v9')

    nodes = {}
    edges = []

    # 핵심 모듈들
    core_modules = {
        'executor': 'engine/skills/executor.py',
        'executor_gotcha': 'engine/skills/executor_gotcha.py',
        'executor_heartbeat': 'engine/skills/executor_heartbeat.py',
        'css_tokens': 'engine/skills/codegen/css_tokens.py',
        'executor_cascade': 'engine/skills/executor_cascade.py',
        'model_adapter': 'engine/ai/model_adapter.py',
        'account_router': 'engine/ai/account_router.py',
        'context_assembler': 'engine/ai/context_assembler.py',
        'dag_advancer': 'engine/core/dag_advancer.py',
        'state_machine': 'engine/core/state_machine.py',
        'cascade': 'engine/core/cascade.py',
        'budget_enforcer': 'engine/core/budget_enforcer.py',
        'startup': 'engine/lifecycle/startup.py',
        'saver': 'engine/skills/artifact/saver.py',
        'thresholds': 'engine/config/thresholds.py',
    }

    # 노드 생성
    node_sizes = {
        'executor': 4772,
        'executor_gotcha': 77,
        'executor_heartbeat': 76,
        'css_tokens': 47,
        'executor_cascade': 800,
        'model_adapter': 600,
        'dag_advancer': 1200,
        'state_machine': 800,
        'cascade': 500,
    }

    for name, path in core_modules.items():
        file_path = v9_path / path
        imports = extract_imports(file_path) if file_path.exists() else []
        size = node_sizes.get(name, 300)

        nodes[name] = {
            'id': name,
            'label': f"{name}\n({size:,} lines)",
            'size': max(300, size // 20),
            'imports': imports,
            'path': path
        }

    # 엣지 생성 (V9 구조 기반)
    dependencies = {
        'executor': ['executor_gotcha', 'executor_heartbeat', 'css_tokens', 'executor_cascade', 'model_adapter', 'thresholds'],
        'executor_gotcha': ['thresholds'],
        'executor_heartbeat': ['thresholds'],
        'executor_cascade': ['cascade', 'budget_enforcer'],
        'model_adapter': ['account_router', 'thresholds'],
        'account_router': ['thresholds'],
        'context_assembler': ['model_adapter', 'startup'],
        'dag_advancer': ['state_machine', 'budget_enforcer', 'cascade'],
        'state_machine': ['cascade'],
        'saver': ['executor'],
        'startup': ['model_adapter', 'budget_enforcer', 'cascade'],
    }

    for source, targets in dependencies.items():
        for target in targets:
            if target in nodes:
                edges.append({
                    'source': source,
                    'target': target,
                    'relation': 'imports',
                    'confidence': 'EXTRACTED'
                })

    return nodes, edges

def generate_react_flow_html(nodes, edges):
    """React Flow HTML 생성."""

    nodes_data = []
    for name, node in nodes.items():
        nodes_data.append({
            'id': name,
            'data': {
                'label': node['label'],
                'path': node['path']
            },
            'position': {'x': 0, 'y': 0},
        })

    edges_data = []
    for edge in edges:
        edges_data.append({
            'id': f"{edge['source']}-{edge['target']}",
            'source': edge['source'],
            'target': edge['target'],
            'animated': True,
        })

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>V9 엔진 아키텍처</title>
    <script crossorigin src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
    <script crossorigin src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
    <script src="https://unpkg.com/reactflow@11/dist/umd/reactflow.production.min.js"></script>
    <link rel="stylesheet" href="https://unpkg.com/reactflow@11/dist/style.css">
    <style>
        body {{ margin: 0; padding: 0; font-family: Arial, sans-serif; }}
        #root {{ width: 100vw; height: 100vh; }}
        .react-flow {{ width: 100%; height: 100%; }}
        .react-flow__node {{
            border: 2px solid #333;
            border-radius: 8px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            font-weight: bold;
            padding: 10px;
            text-align: center;
            font-size: 12px;
            min-width: 120px;
        }}
        .react-flow__node:hover {{
            box-shadow: 0 0 20px rgba(102, 126, 234, 0.8);
            transform: scale(1.05);
        }}
        .react-flow__edge {{
            stroke-width: 2;
            stroke: #667eea;
        }}
        .react-flow__edge.animated {{
            animation: dash 20s linear infinite;
        }}
        @keyframes dash {{
            0% {{ stroke-dashoffset: 10; }}
            100% {{ stroke-dashoffset: 0; }}
        }}
        .controls {{
            position: absolute;
            bottom: 20px;
            left: 20px;
            background: white;
            padding: 10px 15px;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            z-index: 10;
            font-size: 12px;
        }}
        .legend {{
            position: absolute;
            top: 20px;
            left: 20px;
            background: white;
            padding: 15px;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            z-index: 10;
            max-width: 300px;
        }}
        .legend h3 {{ margin: 0 0 10px 0; font-size: 14px; }}
        .legend p {{ margin: 5px 0; font-size: 11px; color: #666; }}
    </style>
</head>
<body>
    <div id="root"></div>
    <div class="legend">
        <h3>🏗️ V9 엔진 아키텍처</h3>
        <p><strong>색상:</strong> 모듈 중요도</p>
        <p><strong>크기:</strong> 코드량</p>
        <p><strong>화살표:</strong> 의존성</p>
        <p><strong style="color: #667eea;">마우스:</strong> 드래그/줌</p>
    </div>

    <script>
        const {{ useState, useCallback }} = React;
        const {{ ReactFlow, Background, Controls, MiniMap, useNodesState, useEdgesState, MarkerType }} = window.reactflow;

        const initialNodes = {str(nodes_data)};
        const initialEdges = {str(edges_data)}.map(e => ({{
            ...e,
            markerEnd: {{ type: MarkerType.ArrowClosed }},
            animated: true,
        }}));

        function Flow() {{
            const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
            const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);

            // Auto-layout (간단한 계층형)
            const layoutNodes = () => {{
                const layers = {{}};
                const visited = new Set();

                const dfs = (nodeId, depth = 0) => {{
                    if (visited.has(nodeId)) return;
                    visited.add(nodeId);

                    if (!layers[depth]) layers[depth] = [];
                    layers[depth].push(nodeId);

                    initialEdges
                        .filter(e => e.source === nodeId)
                        .forEach(e => dfs(e.target, depth + 1));
                }};

                dfs('executor');

                const newNodes = initialNodes.map(node => {{
                    let depth = 0;
                    for (const d in layers) {{
                        if (layers[d].includes(node.id)) {{
                            depth = parseInt(d);
                            break;
                        }}
                    }}
                    return {{
                        ...node,
                        position: {{
                            x: (depth * 250),
                            y: (layers[depth] ? layers[depth].indexOf(node.id) * 150 : 0)
                        }}
                    }};
                }});

                setNodes(newNodes);
            }};

            React.useEffect(() => {{
                layoutNodes();
            }}, []);

            return (
                <ReactFlow
                    nodes={{nodes}}
                    edges={{edges}}
                    onNodesChange={{onNodesChange}}
                    onEdgesChange={{onEdgesChange}}
                >
                    <Background color="#aaa" gap={{16}} />
                    <Controls />
                    <MiniMap />
                </ReactFlow>
            );
        }}

        const root = ReactDOM.createRoot(document.getElementById('root'));
        root.render(<Flow />);
    </script>
</body>
</html>"""

    return html

if __name__ == '__main__':
    nodes, edges = analyze_v9_structure()
    html = generate_react_flow_html(nodes, edges)

    output_file = '/Users/codename/Downloads/v9/v9_architecture.html'
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(html)

    print(f"✅ React Flow HTML 생성 완료: {output_file}")
    print(f"📊 분석 결과:")
    print(f"   - 노드: {len(nodes)}개")
    print(f"   - 의존성: {len(edges)}개")
    print(f"\n🚀 브라우저에서 열기:")
    print(f"   open {output_file}")
