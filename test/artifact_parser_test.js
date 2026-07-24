/**
 * Artifact Parser (Phase 1)
 * 
 * 역할을 분리한 세션 핸드오버 프로토콜 검증을 위한 테스트 구현체.
 * Assistant 메시지에서 Mermaid, Code, Table 에셋을 추출함.
 */

/**
 * 메시지 본문을 파싱하여 첫 번째로 발견되는 Artifact를 반환합니다.
 * @param {string} content - 메시지 본문
 * @returns {Object|null} - 파싱된 Artifact 객체 또는 null
 */
function parseArtifact(content) {
    if (!content) return null;

    // 1. Mermaid 감지 (우선순위 1)
    const mermaidRegex = /```mermaid\n([\s\S]+?)```/;
    const mermaidMatch = content.match(mermaidRegex);
    if (mermaidMatch) {
        return {
            type: 'mermaid',
            content: mermaidMatch[1].trim()
        };
    }

    // 2. 일반 코드 블록 감지 (우선순위 2, 3줄 이상인 경우만)
    const codeRegex = /```(\w*)\n([\s\S]+?)```/g;
    let match;
    while ((match = codeRegex.exec(content)) !== null) {
        const lang = match[1] || 'text';
        const codeContent = match[2].trim();
        const lineCount = codeContent.split('\n').length;

        if (lineCount >= 3) {
            return {
                type: 'code',
                lang: lang,
                content: codeContent
            };
        }
    }

    // 3. 마크다운 테이블 감지 (우선순위 3, |로 시작하는 줄 3개 이상)
    const tableLines = [];
    const lines = content.split('\n');
    for (let i = 0; i < lines.length; i++) {
        const line = lines[i].trim();
        if (line.startsWith('|')) {
            tableLines.push(line);
        } else if (tableLines.length >= 3) {
            break; // 테이블 종료
        } else {
            tableLines.length = 0; // 초기화
        }
    }

    if (tableLines.length >= 3) {
        return {
            type: 'table',
            content: tableLines.join('\n')
        };
    }

    return null;
}

// --- 테스트 케이스 ---

const testMessages = {
    mermaid: `여기 차트 하나 그려줄게:
\`\`\`mermaid
graph TD
    A[Start] --> B{Is it working?}
    B -- Yes --> C[Great!]
    B -- No --> D[Debug]
\`\`\``,

    longCode: `이건 3줄 이상의 코드야:
\`\`\`javascript
function hello() {
    console.log("Hello World");
    return true;
}
\`\`\``,

    shortCode: `이건 짧은 코드(무시되어야 함):
\`\`\`javascript
const a = 1;
\`\`\``,

    table: `데이터를 표로 정리했어:
| ID | Name | Role |
|---|---|---|
| 1 | Prana | Orchestrator |
| 2 | Coder | Worker |`
};

console.log("=== Artifact Parser Test ===");

Object.entries(testMessages).forEach(([key, msg]) => {
    const result = parseArtifact(msg);
    console.log(`\n[Test: ${key}]`);
    if (result) {
        console.log(`Type: ${result.type}`);
        if (result.lang) console.log(`Lang: ${result.lang}`);
        console.log(`Content length: ${result.content.length}`);
        console.log(`Sample: ${result.content.split('\n')[0]}...`);
    } else {
        console.log("Result: NULL (Expected for shortCode)");
    }
});
