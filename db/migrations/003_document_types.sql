-- ============================================================================
-- 003_document_types — allow the file types the pipeline can actually parse.
--
-- Migration 001 widened `type` to pdf/markdown/text. Word, Excel, PowerPoint
-- and image support were added to the parsers and to the upload validator
-- afterwards, but this CHECK was never widened to match — so every .docx
-- upload was rejected by the database and surfaced as a 500.
--
-- Idempotent, like every migration here: the API applies them all on boot.
-- ============================================================================

ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_type_check;
ALTER TABLE documents ADD CONSTRAINT documents_type_check CHECK (type IN (
    'pdf',
    'markdown',
    'text',
    'word',
    'excel',
    'powerpoint',
    'image'
));
