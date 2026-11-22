# Optional Breeze sync

Church Check-in works locally without an external service. Breeze ChMS sync is retained as an optional legacy integration for congregations that still use it. Configure it only if you have an active Breeze account and API key.

## Connect

1. Open Admin and choose **Sync**.
2. Enter the Breeze API address and key supplied by your Breeze administrator.
3. Use **Check** to review the proposed roster and attendance changes.
4. Run a sync manually, or enable the schedule if your congregation wants automatic sync.

Store API keys only in the app's protected data directory. Disconnecting removes the saved key. Make a backup before a manual sync, and review the preview before applying changes.

## Services and attendance

For attendance to sync, the local service and Breeze event must correspond. Use a generic example such as **Sunday Service** while testing. Configure the actual event names in Admin for your own congregation.

## Visitor registrations

Visitor registration is local by default. When sync is configured, supported visitor details can be sent to Breeze according to the selected settings. Review consent wording and data retention choices with your congregation before enabling this feature.

## Troubleshooting

- Confirm that the API address and key are current.
- Check that the computer can reach Breeze.
- Compare the event name and date in Admin with the event in Breeze.
- Make a backup before retrying a sync after an interrupted connection.

The integration uses the Breeze API. Other roster systems require a separate connector.
