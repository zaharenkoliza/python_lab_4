-- Детерминированный датасет: 10 000 строк, без random()
CREATE TABLE items (id integer PRIMARY KEY, name text NOT NULL, payload text NOT NULL);
INSERT INTO items
SELECT i, 'item-' || i, md5(i::text) || md5((i*7)::text) FROM generate_series(1, 10000) AS i;
ANALYZE items;
