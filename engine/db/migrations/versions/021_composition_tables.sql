-- 021_composition_tables: v10 컴포넌트 조합 모델 — 토큰/컴포넌트/레시피 저장소
-- UP:

-- 프로젝트별 디자인 토큰 (1:1)
CREATE TABLE IF NOT EXISTS composition_tokens (
  id              TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  data            TEXT NOT NULL,           -- JSON (DesignTokens 직렬화)
  content_hash    TEXT NOT NULL,           -- 변경 감지용 SHA256 prefix
  version         INTEGER NOT NULL DEFAULT 1,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  UNIQUE(project_id)                      -- 프로젝트당 하나
);

-- 프로젝트별 컴포넌트 라이브러리 (1:N)
CREATE TABLE IF NOT EXISTS composition_components (
  id              TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name            TEXT NOT NULL,           -- 컴포넌트 이름 (hero_header, data_table 등)
  category        TEXT NOT NULL DEFAULT '',-- layout, content, data, form, navigation, feedback
  data            TEXT NOT NULL,           -- JSON (Component 직렬화)
  version         INTEGER NOT NULL DEFAULT 1,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  UNIQUE(project_id, name)                -- 프로젝트 내 이름 유니크
);

CREATE INDEX IF NOT EXISTS idx_comp_components_project
  ON composition_components(project_id);

CREATE INDEX IF NOT EXISTS idx_comp_components_category
  ON composition_components(project_id, category);

-- 프로젝트별 페이지 레시피 (1:N)
CREATE TABLE IF NOT EXISTS composition_recipes (
  id              TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  page_slug       TEXT NOT NULL,           -- URL-safe 페이지 식별자
  page_name       TEXT NOT NULL DEFAULT '',-- 한글 페이지 이름
  data            TEXT NOT NULL,           -- JSON (PageRecipe 직렬화)
  version         INTEGER NOT NULL DEFAULT 1,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  UNIQUE(project_id, page_slug)           -- 프로젝트 내 슬러그 유니크
);

CREATE INDEX IF NOT EXISTS idx_comp_recipes_project
  ON composition_recipes(project_id);

-- DOWN:
-- DROP TABLE IF EXISTS composition_recipes;
-- DROP TABLE IF EXISTS composition_components;
-- DROP TABLE IF EXISTS composition_tokens;
