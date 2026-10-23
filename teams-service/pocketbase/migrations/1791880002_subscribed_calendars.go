package migrations

import (
	"github.com/pocketbase/pocketbase/core"
	m "github.com/pocketbase/pocketbase/migrations"
)

func init() {
	m.Register(func(app core.App) error {
		c, err := app.FindCollectionByNameOrId("calendar_events")
		if err != nil {
			return err
		}
		c.Fields.Add(&core.TextField{Name: "calendar_id", Max: 2000})
		c.RemoveIndex("idx_calendar_events_event")
		c.AddIndex("idx_calendar_events_calendar_event", true, "`calendar_id`, `event_id`", "")
		return app.Save(c)
	}, func(app core.App) error {
		c, err := app.FindCollectionByNameOrId("calendar_events")
		if err != nil {
			return err
		}
		c.RemoveIndex("idx_calendar_events_calendar_event")
		c.AddIndex("idx_calendar_events_event", true, "`event_id`", "")
		c.Fields.RemoveByName("calendar_id")
		return app.Save(c)
	})
}
