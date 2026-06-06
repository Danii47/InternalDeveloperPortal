/// <reference types="astro/client" />

declare namespace App {
  interface Locals {
    user: { userid: string; realm: string };
  }
}
