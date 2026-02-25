export interface ITransport {
  send(message: object): void;
  onMessage(cb: (msg: any) => void): void;
  close(): void;
}
