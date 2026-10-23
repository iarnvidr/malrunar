// Browser (web push) subscriptions and detector state, for a web app that sends notifications about
// the copied chats. The Teams service itself sends no push messages; it only keeps the schema in its
// database. These collections are superuser-only, like the Teams copy: subscription endpoints are credentials.
package migrations

import (
	"github.com/pocketbase/pocketbase/core"
	m "github.com/pocketbase/pocketbase/migrations"
)

func init() {
	m.Register(func(app core.App) error {
		if err := collection(app, "push_subscriptions", map[string]string{"uendpoint_hash": "`endpoint_hash`"},
			text("endpoint_hash", 64, true), text("endpoint", 4000, true), jsonField("subscription", 12000),
			text("device", 100, false), text("owner", 32, true), number("created_at", false),
		); err != nil {
			return err
		}
		return collection(app, "push_state", map[string]string{"uk": "`k`"},
			text("k", 120, true), jsonField("v", 1000000),
		)
	}, func(app core.App) error {
		for _, name := range []string{"push_state", "push_subscriptions"} {
			if c, err := app.FindCollectionByNameOrId(name); err == nil {
				if err := app.Delete(c); err != nil {
					return err
				}
			}
		}
		return nil
	})
}
