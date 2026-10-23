// Local copy of the work Teams chats. Only the Teams service (as superuser)
// reads or writes these collections, so no collection has API rules: PocketBase
// leaves rule-less collections superuser-only.
package migrations

import (
	"github.com/pocketbase/pocketbase/core"
	m "github.com/pocketbase/pocketbase/migrations"
)

func text(name string, max int, required bool) *core.TextField {
	return &core.TextField{Name: name, Max: max, Required: required}
}

func number(name string, onlyInt bool) *core.NumberField {
	return &core.NumberField{Name: name, OnlyInt: onlyInt}
}

func jsonField(name string, maxSize int64) *core.JSONField {
	return &core.JSONField{Name: name, MaxSize: maxSize}
}

func collection(app core.App, name string, indexes map[string]string, fields ...core.Field) error {
	c := core.NewBaseCollection(name)
	c.Fields.Add(fields...)
	for index, columns := range indexes {
		unique := len(index) > 0 && index[0] == 'u'
		c.AddIndex("idx_"+name+"_"+index[1:], unique, columns, "")
	}
	return app.Save(c)
}

func init() {
	m.Register(func(app core.App) error {
		if err := collection(app, "chats", map[string]string{"uchat_id": "`chat_id`", "ilast_at": "`last_at`"},
			text("chat_id", 400, true), text("kind", 40, false), text("title", 1000, false), text("topic", 1000, false),
			text("preview", 1000, false), text("preview_from", 300, false), number("last_at", false), number("unread", true),
			number("read_horizon", false), text("last_msg_id", 80, false), text("last_updated_key", 200, false),
			jsonField("members", 400000), number("member_count", true), text("roster_version", 80, false),
			text("members_roster", 80, false), &core.BoolField{Name: "hidden"}, &core.BoolField{Name: "is_self"},
			text("backfill_link", 8000, false), &core.BoolField{Name: "backfill_done"}, number("oldest_ts", false),
			number("newest_ts", false), number("message_count", true), number("synced_at", false),
			number("rechecked_at", false), text("search", 20000, false),
		); err != nil {
			return err
		}
		if err := collection(app, "messages", map[string]string{"uchat_msg": "`chat_id`, `msg_id`", "ichat_ts": "`chat_id`, `ts`", "its": "`ts`"},
			text("chat_id", 400, true), text("msg_id", 80, true), number("ts", false), text("version", 80, false),
			text("kind", 80, false), text("from_mri", 200, false), text("from_name", 300, false), &core.BoolField{Name: "mine"},
			text("content", 3000000, false), text("search", 400000, false), jsonField("props", 1000000),
			jsonField("reactions", 400000), jsonField("files", 400000), jsonField("media", 100000),
			number("edited_at", false), &core.BoolField{Name: "deleted"}, number("deleted_at", false),
			text("root_id", 80, false), text("subject", 1000, false), number("synced_at", false),
		); err != nil {
			return err
		}
		if err := collection(app, "people", map[string]string{"umri": "`mri`"},
			text("mri", 200, true), text("name", 300, false), text("email", 300, false), number("updated_at", false),
		); err != nil {
			return err
		}
		if err := collection(app, "media", map[string]string{"ukey": "`key`", "istatus": "`status`"},
			text("key", 80, true), text("url", 4000, false), text("chat_id", 400, false), text("msg_id", 80, false),
			text("content_type", 200, false), number("size", true), number("width", true), number("height", true),
			text("path", 400, false), text("status", 40, false), number("attempts", true), text("error", 500, false),
			number("fetched_at", false),
		); err != nil {
			return err
		}
		return collection(app, "state", map[string]string{"uk": "`k`"},
			text("k", 120, true), jsonField("v", 400000),
		)
	}, func(app core.App) error {
		for _, name := range []string{"state", "media", "people", "messages", "chats"} {
			if c, err := app.FindCollectionByNameOrId(name); err == nil {
				if err := app.Delete(c); err != nil {
					return err
				}
			}
		}
		return nil
	})
}
