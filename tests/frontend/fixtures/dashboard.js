// API-shaped fixtures only. Production screens continue to read the server.
export function dashboardFixture() {
  const config = {
    locale: {
      country: 'IN',
      currency: 'INR',
      currency_symbol: '₹',
      timezone: 'Asia/Kolkata',
      clock: '12',
      date_format: 'DMY',
    },
    identity: { display_name: 'The Grove', agent_name: 'Meera' },
    capacity: { enabled: true, sellable_pct: 0.8, slot_minutes: 30, turn_minutes: 90 },
    booking_window: { mode: 'rolling', days: 7 },
    voice: { voice_id: 'meera', languages: ['English'] },
    policy: {},
    messaging: {},
    agent: {},
    features: {},
    telling_guest: {},
  };
  const rules = [
    {
      weekday: 4,
      start_time: '12:00',
      end_time: '22:00',
      total_units: 40,
      slot_minutes: 30,
      turn_minutes: 90,
      label: 'Dinner',
    },
  ];
  const booking = {
    id: 'b1',
    name: 'Priya Rao',
    phone: '+919876543210',
    party_size: 4,
    status: 'confirmed',
    time: '7:00 PM',
    start_time: '2026-09-18T19:00:00+05:30',
    block: 'Dinner',
    reference: 'TL-104',
    visits: 2,
    no_shows: 0,
    notes: 'Window table',
  };
  return {
    '/api/bootstrap': {
      business: {
        id: 'venue1',
        slug: 'grove',
        name: 'The Grove',
        timezone: 'Asia/Kolkata',
        unit_plural: 'covers',
        unit_singular: 'cover',
        tracks_capacity: true,
      },
      locale: config.locale,
      config,
      capacity_rules: rules,
      today: '2026-09-18',
      window_last_day: '2026-09-24',
      holidays_supported: true,
    },
    '/api/locales': {
      countries: [
        {
          code: 'IN',
          name: 'India',
          currency: 'INR',
          currency_name: 'Indian rupee',
          timezone: 'Asia/Kolkata',
          clock: '12',
        },
      ],
      languages: ['English'],
    },
    '/api/bookings': {
      title: 'Tonight',
      bookings: [booking],
      blocks: [{ label: 'Dinner', capacity: 32, committed: 4 }],
      summary: { covers: 4, count: 1, seated: 0, unit_plural: 'covers' },
      last_seating: '9:30 PM',
    },
    '/api/bookings/pending': { pending: null, count: 0 },
    '/api/menu': {
      items: [],
      reader_configured: true,
      upload: { id: 'photo1', content_type: 'image/jpeg' },
    },
    '/api/menu/upload/photo1/read': {
      dishes: [
        { name: 'Goan fish curry', price: 420, section: 'Mains', tags: ['Fish'] },
        { name: 'Mushroom xacuti', price: 360, section: 'Mains', tags: ['Vegan'] },
      ],
    },
    '/api/settings': { config, capacity_rules: rules, changes: [] },
    '/api/stats': { rows: [] },
    '/api/calendar': {
      today: '2026-09-18',
      tracks_capacity: true,
      window_last_day: '2026-09-24',
      days: Array.from({ length: 30 }, (_, i) => ({
        date: `2026-09-${String(i + 1).padStart(2, '0')}`,
        in_window: true,
        closed: false,
        capacity: 32,
        peak: 4,
        covers: 4,
        bookings: 1,
      })),
    },
    '/api/calendar/2026-09-18': {
      date: '2026-09-18',
      tracks_capacity: true,
      closed: false,
      capacity: 32,
      covers: 4,
      booking_count: 1,
      bookings: [booking],
      services: [],
    },
    '/api/blocked': { closed_weekdays: [], rows: [], holidays_supported: true },
    '/api/guests': { guests: [] },
    '/api/calls': { calls: [] },
    '/voices': { known: { meera: 'Warm and clear' }, current: 'meera' },
  };
}

export const pendingBooking = (id = 'p1') => ({
  pending: {
    id,
    name: 'Asha Shah',
    time: '8:00 PM',
    why: 'A party of 10 needs your approval.',
    deadline: '7:30 PM',
    alternative: '',
  },
  defaults: { on_accept: 'text' },
  count: 1,
});
