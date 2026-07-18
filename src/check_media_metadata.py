"""Print the active Windows media session and its metadata.

Run from the project root:
    py src/check_media_metadata.py

Start playback in Chrome, Spotify, or another media app before running it.
"""

import asyncio
import sys
import traceback


async def main():
    try:
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager,
        )
    except Exception as exc:
        print(f"Could not import the WinRT media-control package: {type(exc).__name__}: {exc}")
        return 1

    try:
        manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
        session = manager.get_current_session()
        if session is None:
            print("No active Windows media session was found.")
            print("Start media playback, then run this script again.")
            return 2

        properties = await session.try_get_media_properties_async()
        playback = session.get_playback_info()

        print("Windows media session found")
        print(f"Source:   {session.source_app_user_model_id or '(not supplied)'}")
        print(f"Status:   {playback.playback_status}")
        print(f"Title:    {properties.title or '(not supplied)'}")
        print(f"Artist:   {properties.artist or '(not supplied)'}")
        print(f"Album:    {properties.album_title or '(not supplied)'}")
        print(f"Track #:  {properties.track_number}")
        if properties.thumbnail is None:
            print("Artwork:  (not supplied by the media app)")
        else:
            try:
                from winrt.windows.storage.streams import DataReader

                stream = await properties.thumbnail.open_read_async()
                reader = DataReader(stream.get_input_stream_at(0))
                await reader.load_async(stream.size)
                image_data = bytearray(reader.unconsumed_buffer_length)
                reader.read_bytes(image_data)
                print(f"Artwork:  received ({len(image_data):,} bytes)")
            except Exception as exc:
                print(f"Artwork:  could not read it ({type(exc).__name__}: {exc})")
        return 0
    except Exception as exc:
        print(f"WinRT media-session call failed: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return 3


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
