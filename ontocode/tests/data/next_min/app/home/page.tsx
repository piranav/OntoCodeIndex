'use client';

import { getUser } from "../../lib/user";

export default function HomePage() {
  const user = getUser();
  return <div>{user.name}</div>;
}
